# ADR-011: Lexical retrieval only — reversing ADR-008's hybrid

- **Status:** accepted. **Supersedes the hybrid decision in [ADR-008](008-pgvector-not-a-vector-database.md)**; the rest of ADR-008 (pgvector rather than a vector database, RRF rather than weighted blending, citations as provenance) still stands as reasoning, and is now moot in practice.
- **Date:** 2026-07-30

## Context

ADR-008 chose hybrid retrieval — pgvector for meaning, `tsvector` for exact
tokens, fused by Reciprocal Rank Fusion — and justified it with a measurement:

| configuration | recall@3 |
|---|---|
| dense only | 0.92 |
| lexical only | 0.92 |
| **hybrid, RRF** | **1.00** |

It also wrote, correctly: *"the two halves miss **different** questions. That is
the argument for fusion arriving as evidence rather than as a preference, and it
is now a test."*

The measurement was real. The **questions** were not.

All twelve were written alongside the chunker, by the same hand, in FAO-56's own
vocabulary — *"readily available water RAW and the management allowed depletion
fraction p"*. No grower asks that. Fifteen questions phrased the way a grower or
an agronomist actually asks — *"how do I know when the vines actually need
watering"* — produced this:

| retriever | original 12 | paraphrase 15 | **all 27** |
|---|---|---|---|
| hybrid | 1.00 | 0.47 | 0.70 |
| dense only | 0.92 | 0.53 | 0.70 |
| **lexical only** | 0.92 | **0.67** | **0.78** |

**Lexical alone beats the hybrid**, and not marginally: two questions out of 27,
concentrated entirely in the hard half. The dense retriever was not failing to
earn its place — it was *costing* recall. A weak static embedder answers a hard
query with semantically-plausible-but-wrong passages, and RRF gives them enough
rank to displace correct lexical hits.

Before reversing, the obvious repair was measured too — a stronger embedder:

| model | dim | all 27 | paraphrase |
|---|---|---|---|
| `potion-base-8M` | 256 | 0.70 | 0.47 |
| `potion-base-32M` | 512 | 0.74 | 0.53 |
| `potion-retrieval-32M` | 512 | 0.70 | 0.47 |

Four times the model, twice the vector width, a schema migration — for **one
extra question out of 27**. The retrieval-tuned variant buys nothing at all.

## Decision

**Retrieval is Postgres full-text search over `corpus_chunks.text`. No embedder,
no vectors written, no model in the image.**

`corpus_chunks.embedding` and `embedding_model` remain as reserved, never-written
columns, and the `vector` extension stays installed.

## Rationale

### Because the numbers say so, and ADR-003 is the rule

*Complexity must earn its place.* A component that makes the metric **worse**
while adding a model dependency, 258 MB of image, a Hugging Face download at
build time, an `EMBEDDING_DIM` constant welded into the schema and a `RAG=1`
build flag has not earned anything. Deleting it is not a compromise; it is the
rule being applied to a decision that had escaped it.

### Because the corpus is one English manual

Dense retrieval earns its place where lexical fails outright: synonym gaps across
languages, a corpus of many documents with inconsistent terminology, queries with
no token overlap. This corpus is a single English agronomy manual, and the queries
are about the domain it covers — so *some* lexical overlap is essentially
guaranteed. "Water", "soil", "rain", "wind" appear in both the grower's phrasing
and the manual's.

The `|`-joined `tsquery` matters more than the embedder ever did: `plainto_tsquery`
AND-joins lexemes, so a nine-word question matched **0 of 798** chunks. One
character — `&` to `|` — took lexical-only recall from 0.33 to 0.67 on the hard
half. The entire vector pipeline was compensating for a one-character bug in the
retriever beside it.

### Because the gate caught it, which is the gate working

Phase 15 built `recall@k` against a labelled set precisely because *"retrieval
quality is the kind of claim that rots quietly"*. It rotted immediately — the gate
was scoring 1.00 from the day it was written, and a gate that never moves has
stopped measuring. Adding harder questions is what a gate is *for*, and the first
thing it did was overturn the decision that created it.

### Why the columns stay

ADR-008's revisit trigger is specific and still stands: a corpus past roughly 10⁵
chunks, or one spanning languages. Keeping two nullable unused columns and an
installed extension makes that an ingest away rather than a migration away, at a
cost of one comment. Dropping and re-adding them would be churn dressed as
tidiness.

A test asserts `ingest` writes no vectors, so the columns cannot be quietly
refilled without the measurement above being redone.

## Consequences

**We accept:**

- **No semantic matching at all.** A query with genuinely zero lexical overlap
  retrieves nothing, and `retrieve_for` returns an empty list — which is phase
  15's fail-open floor, so the advisory still ships without citations. On this
  corpus and this domain that case has not been observed; on a second corpus it
  might be the common one.
- **Two reserved columns and an unused extension.** Visible dead schema, with a
  comment saying why and a test keeping it dead.
- **`ts_rank_cd` scores are not comparable across queries.** They order; they do
  not measure. No threshold may be applied to one, and `Hit.score` says so.

**We get:**

- **Better recall**: 0.78 against 0.70 overall, 0.67 against 0.47 on the questions
  that resemble real ones.
- **The `app` image back to 391 MB** from 649 — the model weights, `numpy`,
  `tokenizers`, `hf_xet` and `huggingface_hub` all go.
- **No model download in any build**, and no `HF_HUB_OFFLINE` guard needed to stop
  a nightly CronJob reaching a model hub at 02:00.
- **One retriever to reason about.** Retrieval is now a SQL query an operator can
  paste into psql, and its ranking is explainable without a vector.
- **The `rag` extra deleted** — one fewer axis in the build matrix.

## Revisit if

- A second corpus arrives, especially in another language or with different
  terminology — that is the case dense retrieval exists for, and the benchmark
  above should be re-run rather than assumed.
- The corpus passes ~10⁵ chunks, where lexical ranking quality degrades and
  ADR-008's original trigger applies.
- Paraphrase recall matters more than it does today. The honest first move then is
  **better chunking**, not a bigger embedder: the measurement above shows the
  bottleneck is not the vectors.
