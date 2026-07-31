"""Retrieval over FAO-56, and the line it must not cross.

The corpus is the document `features.py` implements, which is the point and also
the danger. It contains Kc tables, `RAW = TAW x MAD`, and the depletion
recurrence — the very constants the deterministic core computes with. So this
package is arranged around one rule:

    **Retrieval feeds the explanation. It never feeds a computation.**

`Deps` remains the only source of Kc. What retrieval buys is the sentence *"this
follows FAO-56, Chapter 6"* attached to prose that already reports a number
computed in Python. Nothing here is imported by `features.py`, `graph.py` or
`deps.py`, and the phase's invariant check is what keeps that true.

Four modules:

  `corpus`     the committed JSONL: chunks plus the source record that carries
               the CC BY 4.0 attribution the redistribution depends on.
  `queries`    what to ask, derived from the features already computed -- so the
               retrieval a night performs depends on the state of that night's
               water balance rather than on a fixed string.
  `store`      ingest into Postgres, and the query that reads it back: full-text
               search, OR-joined, ranked by length-normalised `ts_rank_cd`.
               No embedder and no vectors (ADR-011).
  `retrieve`   the facade the agents call. Fails open to *no passages*, never to
               a weak one.

`citations` is the ledger: what was retrieved during one advisory run, collected
the way the cost ledger collects usage, and written to a table as provenance rather than
into the advisory contract.
"""

from vinea.rag.citations import RetrievedPassage, citation_scope, current_citations
from vinea.rag.corpus import Chunk, CorpusSource, load_corpus, load_source
from vinea.rag.queries import irrigation_query, spray_query
from vinea.rag.retrieve import retrieve_for

__all__ = [
    "Chunk",
    "CorpusSource",
    "RetrievedPassage",
    "citation_scope",
    "current_citations",
    "irrigation_query",
    "load_corpus",
    "load_source",
    "retrieve_for",
    "spray_query",
]
