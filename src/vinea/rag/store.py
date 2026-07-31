"""Ingest into Postgres, and the full-text query that reads it back.

One retriever: Postgres full-text search over the passage text. That is a
correction, and it was made against numbers.

ADR-008 chose *hybrid* -- dense vectors alongside lexical, fused by Reciprocal
Rank Fusion -- and measured 1.00 recall@3 to justify it. The measurement was real
and the questions were not: all twelve were written alongside the chunker, in
FAO-56's own vocabulary. Fifteen questions phrased the way a grower speaks
produced this:

    retriever      original 12   paraphrase 15   all 27
    hybrid            1.00           0.47          0.70
    dense only        0.92           0.53          0.70
    lexical only      0.92           0.67          0.78

**Lexical alone beats the hybrid.** The static embedder is weak enough that on a
hard query it injects semantically-plausible-but-wrong passages which displace
correct lexical hits through the fusion. The dense half was not merely failing to
earn its place -- it was costing two questions out of 27.

So it is gone: no embedder, no vectors written, no model in the image. ADR-011
records the reversal and the trigger that would bring it back.

## The `tsquery` that has to be OR, not AND

`plainto_tsquery` joins every lexeme with `&`, so a nine-word question requires
all nine terms in one chunk and matches NOTHING -- measured, 0 of 798 chunks.
Re-joining with `|` and letting `ts_rank_cd` rank by term density and proximity is
the whole retriever. That single character is worth more here than the entire
vector pipeline it now replaces.

## ...which makes the ranking, not the filter, the entire retriever

The OR fixed a query that matched nothing and produced one that matches nearly
everything: a typical question now passes the `@@` filter on **400 to 750 of the
798 chunks**. The `WHERE` clause is barely a filter. Every question's answer is
decided by `ts_rank_cd` alone, so the ranking function's parameters are not tuning
-- they are the retrieval algorithm.

Which is why the third argument is there. `ts_rank_cd` without it sums matched-term
density and returns a number that grows with document length, so a long chunk
outranks a short one for containing the same words more times. Normalisation `2`
divides by document length -- the same correction BM25 exists to make. Measured
over the 27 labelled questions:

    variant                        r@1    r@3    r@5    MRR      (all 27)
    text, no normalisation         0.37   0.78   0.81   0.553
    text, /length                  0.52   0.81   0.85   0.674

    variant                        r@1    r@3    r@5    MRR      (the 15 paraphrases)
    text, no normalisation         0.27   0.67   0.67   0.445
    text, /length                  0.47   0.73   0.73   0.607

r@3 moved by one question, which alone would be noise. r@1 moved by four and MRR
by 22% overall and 36% on the paraphrases, which is not -- the correct passage was
already being retrieved and was being ranked below longer ones.

**No migration.** Normalisation changes only the score, and the score is not
indexed; the GIN index is on the expression in the `WHERE` clause, which is
untouched. A ranking change that needed a schema change would be a warning that
the ranking had been baked into storage.

## The locator stays out of the tsvector, measured

The obvious next lever was indexing `locator || '. ' || text`, since a locator like
"Chapter 8 — ETc under soil water and salinity stress conditions" is real signal
and the dense path had used exactly that string. It was measured and rejected:

    variant                 orig 12   paraphrase 15   all 27   MRR
    text, /length              0.92        0.73         0.81   0.674
    loc+text, no norm          1.00        0.67         0.81   0.640
    loc+text, /length          1.00        0.60         0.78   0.711

It takes the *easy* half to a perfect score and costs two questions on the hard
half -- and the hard half is the one phrased the way a grower speaks. The reason is
visible in the failures: long section titles are mostly common words, so a question
about a windy day ranks "ETc under soil water and salinity stress conditions" first
for containing "water". The locator adds term frequency without adding aboutness,
which is the exact thing length normalisation is there to punish, so the two levers
work against each other.

Same shape as the ADR-011 reversal, one level down: the change that looks
principled scored better on the questions written by the person who built it.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text as sql
from sqlmodel import Session

from vinea.rag.corpus import Chunk

_SEARCH_SQL = sql("""
WITH tsq AS (
    -- OR, not AND. `plainto_tsquery` joins lexemes with `&`, so a nine-word
    -- question requires all nine terms in one chunk and matches nothing.
    -- Rewriting its OUTPUT rather than the user's input is deliberate: Postgres
    -- has already normalised and escaped the lexemes, so nothing user-supplied
    -- reaches `to_tsquery` unparsed.
    --
    -- A CTE because Postgres allows a function call in the FROM clause but not an
    -- arbitrary expression: `plainto_tsquery(...) AS q` parses, `replace(...)::tsquery AS q`
    -- does not.
    SELECT replace(plainto_tsquery('english', :query_text)::text, '&', '|')::tsquery AS query
)
SELECT c.id,
       c.locator,
       c.text,
       -- 2 = divide the rank by the document length. Without it `ts_rank_cd`
       -- returns a raw density that grows with length, so a long chunk outranks a
       -- short one for repeating the same words. Measured: r@1 0.37 -> 0.52, MRR
       -- 0.553 -> 0.674 over the 27 labelled questions. This argument is the
       -- retrieval algorithm, not a tuning knob -- see the module docstring for why
       -- the ranking is doing all the work here.
       ts_rank_cd(to_tsvector('english', c.text), tsq.query, 2) AS score
FROM corpus_chunks AS c, tsq
WHERE c.source = :source
  -- Unchanged, and deliberately so: this is the expression the GIN index is built
  -- on. Normalisation applies to the score alone, so ranking improved without a
  -- migration.
  AND to_tsvector('english', c.text) @@ tsq.query
ORDER BY score DESC, c.id
LIMIT :top_k
""")


@dataclass(frozen=True, slots=True)
class Hit:
    """One result. `score` is a `ts_rank_cd` value: it orders, it does not measure.

    No threshold should ever be applied to one -- the numbers are not comparable
    between queries, only within one.
    """

    chunk_id: int
    locator: str
    text: str
    score: float


def ingest(
    session: Session,
    chunks: tuple[Chunk, ...] | list[Chunk],
    *,
    source: str,
    batch_size: int = 256,
) -> int:
    """Upsert chunks. Does not commit -- transactions belong to the caller.

    Upsert on `(source, chunk_id)`, so re-ingesting the same corpus overwrites
    rather than duplicating: the same idempotency shape as the advisory write, and
    for the same reason. This gets run twice by someone unsure whether the first
    run worked.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from vinea.db.models import CorpusChunk

    chunks = list(chunks)
    written = 0

    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        rows = [
            {
                "source": source,
                "chunk_id": chunk.id,
                "chapter": chunk.chapter,
                "section": chunk.section,
                "locator": chunk.locator,
                "text": chunk.text,
            }
            for chunk in batch
        ]
        statement = pg_insert(CorpusChunk).values(rows)
        statement = statement.on_conflict_do_update(
            constraint="uq_corpus_chunks_natural",
            set_={
                "chapter": statement.excluded.chapter,
                "section": statement.excluded.section,
                "locator": statement.excluded.locator,
                "text": statement.excluded.text,
            },
        )
        session.execute(statement)
        written += len(rows)

    session.flush()
    # The Core upsert bypassed the ORM, so an identity-map copy read earlier in
    # this session is stale. Same debt, same repayment, as `save_advisory`.
    session.expire_all()
    return written


def search(session: Session, query: str, *, source: str, top_k: int = 3) -> list[Hit]:
    """Ranked passages for a query, best first. One round trip, no model."""
    rows = session.execute(
        _SEARCH_SQL, {"query_text": query, "source": source, "top_k": top_k}
    ).all()
    return [Hit(chunk_id=r[0], locator=r[1], text=r[2], score=float(r[3])) for r in rows]
