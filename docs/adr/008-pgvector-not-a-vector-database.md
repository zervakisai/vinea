# ADR-008: pgvector in the database we already run, and hybrid retrieval

- **Status:** **partially superseded by [ADR-011](011-lexical-retrieval-only.md).** The hybrid decision was reversed on 2026-07-30: lexical retrieval alone scored 0.78 recall@3 against the hybrid's 0.70 once the question set included fifteen questions phrased the way a grower asks them. The twelve questions this ADR measured against were written alongside the chunker in the document's own vocabulary, and were too easy. Everything else here — pgvector over a vector database, RRF over weighted blending, citations as provenance, no ANN index — still holds as reasoning and is now moot in practice.
- **Status (original):** accepted
- **Date:** 2026-07-29
- **Milestone:** phase 15 (retrieval & citations)

## Context

The advisories are grounded in *data* and ungrounded in *knowledge*. Phase 1's
`GroundingFact` already forces every rationale point to name the input value
behind it — midday Delta-T was 9.1 °C, here is the column. But when the model
writes "the vine is in mid-season, so Kc is around 0.7", the only provenance is
that a language model said so. An agronomist who disagrees has nothing to check,
and the argument ends at "the model said so".

Meanwhile the system implements FAO-56 faithfully and has never once pointed at
it.

The corpus that fixes this is the awkward one, and the awkwardness is the whole
decision: **FAO-56 contains the constants `features.py` computes with.** Kc
tables, `RAW = p × TAW`, the depletion recurrence. Retrieval over it will surface,
mid-run, a passage stating a crop coefficient for grapes — beside a `Deps.kc` a
human configured and a depletion already computed in millimetres. Two numbers,
same name, different authority.

## Decision

**Retrieval lives in the Postgres we already run: pgvector for meaning,
`tsvector` for exact tokens, fused by Reciprocal Rank Fusion. Embeddings come
from a small static model that needs no credential. Citations are provenance and
live on a table, not on the advisory contract. And no retrieved text may reach a
computed value.**

## Rationale

### Why pgvector rather than a vector database

ADR-003's standing argument applies unchanged: *complexity must earn its place*,
and a second stateful system is a permanent operational cost. It is not defeated
here. Two additional reasons specific to this phase:

**The join is the product.** `advisory_citations` references both `advisories`
and `corpus_chunks`. With a separate vector store those are two systems and the
join happens in application code, over results that cannot be joined in SQL —
so "which passages were shown when we advised this grower" stops being a query.

**Hybrid becomes affordable.** Both retrievers are `SELECT`s against one table.
With a separate store, hybrid means two clients, two rankings, and fusion in
Python. Notice the direction of that argument: the *earlier* decision is what
makes the later one cheap.

### Why hybrid, with numbers

The essay for this phase asserted that lexical search would be "not a fallback
but the better half" for a technical manual. Measured on 12 labelled questions,
`top_k=3`, `potion-base-8M`:

| configuration | recall@3 |
|---|---|
| dense only | 0.92 — misses *wind speed measurement height* |
| lexical only | 0.92 — misses *soil evaporation coefficient Ke* |
| **hybrid, RRF** | **1.00** |

The two halves miss **different** questions. That is the argument for fusion
arriving as evidence rather than as a preference, and it is now a test: if either
half ever dominates the other everywhere, the hybrid has stopped earning its
complexity and the honest move is to delete half the query.

(The first measurement said lexical-only scored 0.33 and appeared to refute the
essay. It was measuring a bug — see *Consequences*.)

### Why Reciprocal Rank Fusion rather than a weighted score blend

A blend has to normalise a cosine distance against a `ts_rank_cd` score: two
numbers on incomparable scales, whose ratio is a tuning constant that gets set
once and never revisited. RRF uses ranks only, so it has no scale to tune. Its
published constant `k = 60` is left alone deliberately — inventing a knob here
would hand the decision back its main drawback.

### Why a deliberately weaker embedding model

The obvious choice is a hosted embedding model through the phase-14 gateway:
better vectors, no new dependency, cost accounting for free. Rejected, and not on
quality.

Phase 12 established that a claim without a gate rots, and retrieval quality is
exactly that kind of claim — it degrades one chunking change at a time and
nothing goes red. So `recall@k` must be gated in CI, and CI has no provider key
(phase 14 established that: it is why the gateway is not deployed in the e2e). An
embedder behind a secret makes the gate unrunnable.

`model2vec`'s `potion-base-8M` is a distilled token matrix plus pooling —
inference is a tokenizer lookup and a mean, in numpy, no torch. ~30 MB, no
credential, runs in CI. **A better embedder behind a secret would score higher on
a number nobody could check.**

### Why no ANN index

Phase 13's `db_tier` variable warns that `db-f1-micro` "is NOT enough for the
phase-15 pgvector work". This phase declines to cash that cheque. The corpus is
798 rows × 256 dimensions ≈ 800 KB of vectors; Postgres scans that exhaustively
in single-digit milliseconds, which is *exact* rather than approximate and costs
no build memory, no index maintenance and no `ef_search` constant. An HNSW index
earns its place somewhere north of 10⁵ rows. Adding one here would be complexity
bought with a guess.

### Why citations are a table and not a contract field

`contracts.py` is protected by the invariant, so `DailyFarmAdvisory` cannot grow
a `citations` field. The invariant forced the question and the project had
already answered it — `repository.get_advisory_row`, phase 6:

> `trace_id`, `degraded` and the prompt tags are *about* the advisory, not part
> of it, and the contract should not grow fields because a storage layer wanted
> them.

A citation is provenance, exactly like `model_id`, `trace_id` and `cost_usd`. It
belongs on the row.

### What is stored is what was *shown*, not what was *used*

`advisory_citations` records the passages retrieval supplied. Asking the model
which sources it used would be a stronger claim and a *self-report* — and phase
12 exists because self-report is not evidence. A model can name a citation it
never read. What retrieval put in front of it is a fact about the run: it cannot
be gamed, needs no model cooperation, and is reproducible from the table alone.

The UI must therefore say **"sources shown to the model"**, never "sources used".
The difference is the entire epistemic content.

## Rejected alternatives

**Qdrant / Weaviate / Chroma / a hosted vector DB.** Better ANN, purpose-built
filtering, and for Chroma almost no operational cost. Rejected by ADR-003's
standing argument plus the join and hybrid arguments above. Revisit at a corpus
size where exhaustive scan stops being viable — and bring the measurement.

**A hosted embedding model via the gateway.** See above: it costs the gate.

**`sentence-transformers` cross-encoder reranking.** The quality move, and it
means torch in an image that is 389 MB today. Rejected on size, and on the
observation that recall@3 is already 1.00 on the labelled set — a reranker
improves ordering *within* results that are already correct. Revisit when the
question set is hard enough for the number to move.

**Letting retrieval inform the computation.** Explicitly rejected, and it is the
reason this ADR exists. A retrieved Kc could "check" the configured one. Then the
water balance has two sources: a config row a human chose and versioned with
`deps_hash`, and whatever an embedding surfaced tonight — not reproducible, not
tagged, invisible in every drift tag. `Deps` wins, always.

## Consequences

**We accept:**

- Two new tables, one new extension (`CREATE EXTENSION vector`), and a Postgres
  image that must carry pgvector — the stock `postgres:16` does not, so the
  compose stack and the test fixture both move to `pgvector/pgvector:pg16`.
  Managed offerings gate the extension behind a flag; a server without it fails
  the pre-upgrade migration hook, loudly, before any new pod serves traffic.
- A ~30 MB model download on first use. No credential, but not nothing.
- **Retrieval quality is now a thing that can silently rot**, which is why the
  gate exists — and the gate is only as good as 12 hand-written questions.
- **A lexical retriever that is easy to build wrong.** `plainto_tsquery` joins
  every lexeme with `&`, so a nine-word question requires all nine terms in one
  chunk and matched **0 of 798**. The lexical half was contributing nothing while
  looking like it worked, and the first recall measurement (0.33 lexical-only)
  measured that bug rather than lexical search. Fixed by re-joining the lexemes
  with `|` and letting `ts_rank_cd` rank.

**We get:**

- Citations a grower or agronomist can check, at chapter and table granularity.
- Retrieval quality gated in CI with no secret.
- An answer to "which passages does this system lean on most", as a SQL query.
- The whole thing off by default: with no corpus ingested, `retrieve_for` returns
  nothing and the advisory is produced exactly as it was in phase 14.

## Revisit if

- The corpus grows past ~10⁵ chunks — the ANN index earns its place, with a
  measurement.
- The labelled question set stops discriminating (everything scores 1.00 forever)
  — that means the gate has stopped measuring, not that retrieval is perfect.
- A second corpus arrives under a different licence — `corpus_chunks.source` is
  open-ended TEXT for exactly that, but `ATTRIBUTION.md` and the fetch script's
  licence assertion are per-source and would need extending.
