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
       ts_rank_cd(to_tsvector('english', c.text), tsq.query) AS score
FROM corpus_chunks AS c, tsq
WHERE c.source = :source
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
