"""Ingest into Postgres, and the hybrid query that reads it back.

Two retrievers over one table, fused by rank:

  **Dense** — pgvector cosine distance over the embedding. Finds passages that
  *mean* what the query means, including ones that never use its words.
  **Lexical** — Postgres full-text over the same `text` column. Finds passages
  containing the exact tokens, which for this corpus is not a fallback but the
  better half: `RAW`, `Kc`, `ETo`, `Table 12` are the terms an agronomist
  actually asks with, and a 256-dimension static embedding blurs them into
  general prose about crop coefficients.

That is not a hypothetical. Measured on this corpus before hybrid existed, the
dense-only top hit for *"readily available water RAW and management allowed
depletion fraction p"* was a list of symbol definitions at cosine 0.496 — the
right chapter, the wrong paragraph, and a citation a grower could not use.

Both halves are `SELECT`s against one table in one database, so hybrid costs no
infrastructure here. Worth noticing that the *earlier* decision is what makes
this one cheap: with a separate vector store (ADR-008's rejected alternative)
hybrid would mean two systems, two client libraries and a join in application
code over results that cannot be joined in SQL.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text as sql
from sqlmodel import Session

from vinea.rag.corpus import Chunk
from vinea.rag.embedding import Embedder

# Reciprocal Rank Fusion's damping constant, from the paper that introduced it.
# 60 is the published default and is left alone deliberately: the whole reason
# RRF was chosen over a weighted score blend is that it has no scale to tune, and
# inventing a tuning knob here would give the decision back its main drawback.
RRF_K = 60

# How deep each retriever goes before fusion. Larger than `top_k` because a
# passage ranked 8th by one retriever and 2nd by the other should be able to win,
# and it cannot if the first retriever only reported five results.
CANDIDATES = 40

_HYBRID_SQL = sql("""
WITH tsq AS (
    -- OR, not AND, and this CTE is the difference between a working lexical
    -- retriever and a decorative one. `plainto_tsquery` joins every lexeme with
    -- `&`, so a nine-word question requires all nine terms in one chunk and
    -- matches NOTHING: measured, 0 of 798 chunks for "readily available water
    -- RAW and the management allowed depletion fraction p". With the lexical
    -- half in that state, lexical-only recall@3 was 0.33.
    --
    -- Re-joining with `|` means "chunks containing any of these terms" (535 of
    -- 798 here) and lets `ts_rank_cd` do the actual work -- it scores by term
    -- density and proximity, which is the ranking signal we wanted all along.
    -- The AND was doing filtering nobody asked for.
    --
    -- Rewriting `plainto_tsquery`'s OUTPUT rather than the user's input is
    -- deliberate: Postgres has already normalised and escaped the lexemes, so
    -- nothing user-supplied reaches `to_tsquery` unparsed.
    --
    -- A CTE rather than an expression in the FROM clause because Postgres allows
    -- a *function call* there but not an arbitrary expression -- `plainto_tsquery(...)
    -- AS query` parses, `replace(...)::tsquery AS query` does not.
    SELECT replace(plainto_tsquery('english', :query_text)::text, '&', '|')::tsquery AS query
),
dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> CAST(:query_vector AS vector)) AS rank
    FROM corpus_chunks
    WHERE source = :source AND embedding IS NOT NULL
    ORDER BY embedding <=> CAST(:query_vector AS vector)
    LIMIT :candidates
),
lexical AS (
    SELECT corpus_chunks.id, ROW_NUMBER() OVER (
               ORDER BY ts_rank_cd(to_tsvector('english', text), tsq.query) DESC, corpus_chunks.id
           ) AS rank
    FROM corpus_chunks, tsq
    WHERE source = :source AND to_tsvector('english', text) @@ tsq.query
    ORDER BY ts_rank_cd(to_tsvector('english', text), tsq.query) DESC, corpus_chunks.id
    LIMIT :candidates
)
SELECT c.id,
       c.locator,
       c.text,
       COALESCE(1.0 / (:rrf_k + dense.rank), 0.0)
     + COALESCE(1.0 / (:rrf_k + lexical.rank), 0.0) AS score
FROM corpus_chunks AS c
LEFT JOIN dense   ON dense.id   = c.id
LEFT JOIN lexical ON lexical.id = c.id
WHERE dense.id IS NOT NULL OR lexical.id IS NOT NULL
ORDER BY score DESC, c.id
LIMIT :top_k
""")


@dataclass(frozen=True, slots=True)
class Hit:
    """One fused result. `score` is an RRF score and is NOT a similarity.

    Named `score` rather than `relevance` on purpose: RRF scores are bounded by
    `2 / RRF_K` and mean nothing in isolation. They order results; they do not
    measure them, and no threshold should ever be applied to one.
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
    embedder: Embedder,
    batch_size: int = 128,
) -> int:
    """Upsert chunks with their vectors. Does not commit (transactions are the caller's).

    Upsert on `(source, chunk_id)`, so re-ingesting the same corpus overwrites
    rather than duplicating — the same idempotency shape as the advisory write,
    and for the same reason: this will be run twice, by a person who is not sure
    whether the first run worked.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from vinea.db.models import CorpusChunk

    chunks = list(chunks)
    written = 0
    model_name = getattr(embedder, "model_name", "unknown")

    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = embedder.encode([c.embedding_text for c in batch])
        rows = [
            {
                "source": source,
                "chunk_id": chunk.id,
                "chapter": chunk.chapter,
                "section": chunk.section,
                "locator": chunk.locator,
                "text": chunk.text,
                "embedding": vector,
                "embedding_model": model_name,
            }
            for chunk, vector in zip(batch, vectors, strict=True)
        ]
        statement = pg_insert(CorpusChunk).values(rows)
        statement = statement.on_conflict_do_update(
            constraint="uq_corpus_chunks_natural",
            set_={
                "chapter": statement.excluded.chapter,
                "section": statement.excluded.section,
                "locator": statement.excluded.locator,
                "text": statement.excluded.text,
                "embedding": statement.excluded.embedding,
                "embedding_model": statement.excluded.embedding_model,
            },
        )
        session.execute(statement)
        written += len(rows)

    session.flush()
    # The Core upsert bypassed the ORM, so any identity-map copy read earlier in
    # this session is stale. Same debt, same repayment, as `save_advisory`.
    session.expire_all()
    return written


def search(
    session: Session,
    query: str,
    *,
    embedder: Embedder,
    source: str,
    top_k: int = 3,
) -> list[Hit]:
    """Hybrid retrieval: dense + lexical, fused by RRF, best first.

    One round trip. Doing the fusion in SQL rather than in Python is not
    micro-optimisation — it keeps the whole ranking expressible as a query an
    operator can run in psql when they want to know why a passage was chosen.
    """
    vector = embedder.encode([query])[0]
    rows = session.execute(
        _HYBRID_SQL,
        {
            # pgvector's text input form. Passed as a string and CAST in SQL so
            # this works without registering a bind processor on a raw connection.
            "query_vector": "[" + ",".join(f"{component:.6f}" for component in vector) + "]",
            "query_text": query,
            "source": source,
            "candidates": CANDIDATES,
            "rrf_k": RRF_K,
            "top_k": top_k,
        },
    ).all()
    return [Hit(chunk_id=r[0], locator=r[1], text=r[2], score=float(r[3])) for r in rows]
