# Phase 15 — Retrieval, and the line it must not cross

`git checkout phase-15`

> The problem statement and decision table below were written **before** the
> build. Everything from *What the measurements said* onward was written after —
> including a correction to a claim in the decision table that turned out to be
> right for the wrong reason, and briefly looked wrong for a better one.

## What you learn

That the hard part of RAG in a system with a deterministic core is not retrieval
quality — it is knowing which fields retrieved text is allowed to touch. And that
"cite your sources" and "cite your inputs" are two different demands that look
identical until one of them starts moving a number.

## The problem

The advisories are grounded in *data* and ungrounded in *knowledge*.

Phase 1 already solved the first half, and solved it well. `contracts.py` carries

```python
class GroundingFact(BaseModel):
    """One cited input behind a rationale point (forces no-invented-numbers)."""
```

so every rationale point names the input value behind it — midday Delta-T was
9.1 °C, here is the column it came from. A grower can check that against the
weather.

But nothing backs the *agronomy*. When the model writes "the vine is in
mid-season, so Kc is around 0.7", or "spraying below 2 m/s risks inversion
drift", those are claims about the world, and their only provenance is that a
language model produced them. Three consequences, in increasing order of how much
they should bother you:

1. **An agronomist who disagrees has nothing to check.** The disagreement stalls
   at "the model said so", which is the worst possible place for an argument
   between a professional and a system to end.
2. **Half-remembered agronomy is indistinguishable from the real thing.** A
   confident sentence about veraison timing reads exactly like a correct one, and
   phase 12's evaluators score irrigation numbers, not botany.
3. **The system implements FAO-56 and cannot point at it.** `features.py` is a
   faithful implementation of a specific published document. The advisories it
   produces never mention it.

## The corpus, and why it is the awkward one

**FAO Irrigation and Drainage Paper 56, *Crop evapotranspiration*, second edition
revised 2025** — the document `features.py` implements.

Licence, checked rather than assumed, from FAO's own repository API:

```
dc.rights.license:  CC BY 4.0
fao.identifier.doi: https://doi.org/10.4060/cd6621en
```

Same licence as the Open-Meteo weather already in `data/`, which keeps
`ATTRIBUTION.md` a single coherent story. FAO also publishes an **extracted plain
text** bitstream (1.05 MB, 19 493 lines) beside the 22 MB PDF, so ingesting it
needs no PDF parser, no `pypdf`, no `unstructured` — and the extraction is FAO's,
not ours, which is one fewer thing to be wrong about.

Now the awkward part, which is the whole reason this phase is interesting.

**The corpus contains the numbers the code computes.** FAO-56 is where Kc tables
live. It is where `RAW = TAW × MAD` comes from. It is where the depletion
recurrence is defined. Retrieval over it will surface, in the middle of a nightly
run, a passage stating a crop coefficient for grapes — while `Deps.kc` holds the
value this grower's block is actually configured with, and `features.py` has
already used it to compute a depletion in millimetres.

Two numbers, same name, different authority. And the tempting move is right
there:

> *We retrieved a Kc table. The configured Kc is 0.70 and the table says 0.70 for
> mid-season wine grapes. Why not have the model reconcile them?*

Because that is how the physics ends up in a vector index. The moment a retrieved
passage can influence a computed value, the water balance has two sources: a
config row a human chose, and whatever an embedding surfaced tonight. The second
one is not reproducible, not versioned by `deps_hash`, and not visible in any
drift tag.

**The rule this phase is built on: retrieval feeds the explanation, never the
computation.** `Deps` remains the only source of Kc. What retrieval buys is the
sentence *"this follows FAO-56 §6.2, which gives 0.70 for mid-season wine
grapes"* — attached to prose that already reports a number computed in Python.

That is the same claim phase 3 made about topology, one layer out. There, the
`FeatureBuilderNode` calls no model. Here, no retrieval result reaches
`FeatureBuilderNode` at all — and the graph must say so structurally, not by
convention.

## The invariant problem this phase creates

The brief for this phase said *citations as an additive contract field*. That
cannot be done, and the reason is the invariant:

```bash
git diff --ignore-blank-lines phase-14 phase-15 -- ... src/vinea/contracts.py   # must be empty
```

`DailyFarmAdvisory` cannot grow a `citations` list. So either the invariant bends,
or citations live somewhere else.

They live somewhere else, and the project has already decided where. From
`repository.get_advisory_row`, written in phase 6:

> Separate from `get_advisory` on purpose: `trace_id`, `degraded` and the prompt
> tags are *about* the advisory, not part of it, and the contract should not grow
> fields because a storage layer wanted them.

A citation is provenance. It is about the advisory, in exactly the way `model_id`,
`trace_id` and (since phase 14) `cost_usd` are about the advisory. It belongs
where provenance belongs: on the row, and in the API envelope — not in the
contract the agents produce.

Which is a better answer than the one the brief asked for, and I would not have
found it by choosing freely. The invariant did the reasoning.

## Decision table

| Question | Options | Verdict |
|---|---|---|
| **Where does the vector index live?** | pgvector on the Postgres we already run · Qdrant/Weaviate/Chroma · a hosted vector DB | **pgvector** — ADR-008 mirrors ADR-003 word for word. A separate vector store is a second stateful system, and the chunks are joinable to `advisories` only if they share a database. The rejected options are rejected by a standing argument, not a fresh preference |
| **Dense, lexical, or both?** | pgvector only · Postgres full-text only · hybrid | **hybrid** — FAO-56 is full of exact tokens (`Kc`, `ETo`, `RAW`, "Table 12") where lexical beats embeddings outright, and full of prose where the reverse holds. Both halves are `SELECT`s against the same database, so hybrid costs no infrastructure here — which is *why* it is affordable, and worth noticing that the answer depends on the earlier decision.<br><br>**Measured afterwards:** dense 0.92, lexical 0.92, hybrid 1.00 — and they miss *different* questions. The prediction held, but only after a bug that had silently disabled the lexical half was found; see **The trap** |
| **How are the two rankings combined?** | weighted score blend · Reciprocal Rank Fusion | **RRF** — a weighted blend requires normalising a cosine distance against a `ts_rank`, two scores on incomparable scales, and the weights become a tuning constant nobody revisits. RRF uses ranks only and has no scale problem |
| **Rerank?** | cross-encoder (`sentence-transformers`) · LLM reranker via the phase-14 gateway · none | **none** — resolved after measuring: recall@3 is already 1.00 on the labelled set, and a reranker improves ordering *within* results that are already correct. A cross-encoder also means torch in an image that is 391 MB. Revisit when the question set is hard enough for the number to move |
| **Which embedding model?** | via the gateway (`text-embedding-3-small`) · a static local model (~30 MB, numpy-only) · sentence-transformers | **static local** — and on the gate, not on quality. CI has no provider key (phase 14 established that), so a hosted embedder makes `recall@k` unrunnable. A better embedder behind a secret would score higher on a number nobody could check |
| **Where do citations live?** | `DailyFarmAdvisory.citations` · a table keyed by `advisory_id` | **a table** — see above. Shaped like `annotations`, which makes "which passages get cited most" a query rather than a JSONB scan |

## What must be gated, and what honestly cannot be

Phase 12 established that a claim without a gate rots. Retrieval quality is
exactly the kind of claim that rots quietly — it degrades one chunking change at
a time and nothing goes red.

So: a labelled question set, and `recall@k` gated in CI. The complication is that
the dense half needs an embedding model, and CI has no provider key (phase 14
established that too — it is why the gateway is not deployed in the e2e).

Three ways out, and the third is the only honest one unless a local embedder
earns its place:

1. Gate on a stub embedder — measures the plumbing and nothing about retrieval.
2. Require a key in CI — makes the pipeline depend on a secret and a vendor.
3. **Gate the lexical half in CI, skip the dense half with a reason.** The
   lexical half needs no model at all, so its recall is fully measurable offline.
   It gates what it can gate and says out loud what it cannot.

If a ~30 MB static embedding model turns out to be good enough, option 3 becomes
unnecessary and the whole gate runs offline. That is a measurement, not a
preference, and it belongs in the build rather than here.

## Open questions for the build — answered

1. **Does the free-tier database survive this?** Yes, and phase 13's warning goes
   uncashed. 798 chunks × 256 dimensions is ~800 KB of vectors; Postgres scans
   that exhaustively in single-digit milliseconds — *exactly*, with no build
   memory and no `ef_search` constant. No index was built. The cheque phase 13
   wrote in advance turned out not to be owed, which is the system working.
2. **Where does chunking put the section headings?** On every chunk, as
   `locator`, and prepended to the embedded text — a paragraph deep inside the
   soil-water chapter may never repeat the phrase "soil water stress", so the
   heading carries topic words the body omits. Getting this wrong is trap 3.
3. **Which node retrieves?** Neither — and `graph.py` did not change. Retrieval
   happens inside `run_irrigation_agent` and `run_spray_agent`, which already
   *receive* computed features as parameters. That makes the ordering structural
   rather than conventional: there is no arrangement of that code in which a
   passage reaches `build_features`, because the features exist before retrieval
   is called. A new graph node would have needed the protected file to change to
   express the same constraint less clearly.
4. **What happens when retrieval finds nothing relevant?** Silence, and there is
   deliberately no "serve a weaker passage" rung. Every other fail-open path here
   degrades toward a correct-but-lesser answer — the deterministic advisory, the
   bundled prompt, a NULL cost. A citation is different: an unfounded one is not a
   *lesser* claim, it is a *stronger* one. `retrieve_for` never raises and returns
   `[]`, and `render_passages([])` is the empty string rather than "no sources
   found" — a sentence about missing sources is itself something a model will try
   to be helpful about.

## The invariant

```bash
git diff --ignore-blank-lines phase-14 phase-15 -- \
  src/vinea/features.py src/vinea/contracts.py src/vinea/deps.py \
  src/vinea/graph.py src/vinea/reconcile.py src/vinea/pipeline.py     # must be empty
```

This is the phase where that command stops being a formality. The corpus contains
the constants; the protected files compute with them; and the entire value of
retrieval here is that it explains the computation without ever participating
in it.

## What the measurements said

Twelve labelled questions, ground truth at chapter granularity, `top_k = 3`,
`potion-base-8M`:

| configuration | recall@3 |
|---|---|
| dense only | 0.92 — misses *wind speed measurement height* |
| lexical only | 0.92 — misses *soil evaporation coefficient Ke* |
| **hybrid, RRF** | **1.00** |

The two halves miss **different** questions. That is the argument for fusion
arriving as evidence instead of preference, and it is now a test of its own: if
either half ever dominates everywhere, the hybrid has stopped earning its
complexity and the honest move is to delete half the query.

Ground truth is a **chapter**, not a chunk id, and that choice is load-bearing.
Chunk ids are assigned in file order by the fetch script, so they move whenever
the chunker or the upstream extraction changes. A gate keyed on them would go red
on every corpus regeneration for reasons unrelated to retrieval quality — and a
gate that cries wolf gets deleted rather than investigated.

The floor is set at **0.91**, one miss below the measured 1.00. A threshold pinned
to a perfect score goes red the first time someone rewords a query, which is the
same failure in a different direction.

## Decisions

**ADR-008.** pgvector in the database we already run; hybrid dense + lexical
fused by RRF; a deliberately weaker embedding model; no ANN index; citations as
provenance.

**The corpus verifies its own licence.** `scripts/fetch_corpus.py` resolves the
item through FAO's repository API and asserts `dc.rights.license == "CC BY 4.0"`
before writing a byte. An attribution claim that lives only in a Markdown file is
a claim nobody rechecks; this one fails the regeneration if it stops being true.
The same record is written as the first line of the JSONL, so a copy of the
corpus separated from `ATTRIBUTION.md` still carries its terms.

**No ANN index, and phase 13's warning goes uncashed.** `db_tier`'s comment says
`db-f1-micro` "is NOT enough for the phase-15 pgvector work". It is, because the
corpus is 798 rows × 256 dimensions ≈ 800 KB of vectors and Postgres scans that
exhaustively in single-digit milliseconds — *exactly*, with no build memory, no
index maintenance and no `ef_search` constant. An HNSW index earns its place
somewhere north of 10⁵ rows. Writing "we will need X" in phase 13 and then
measuring in phase 15 that we do not is the system working.

**The embedding model is baked into the image, not downloaded at runtime.**
`--build-arg RAG=1` fetches the weights at build time and the runtime sets
`HF_HUB_OFFLINE=1`. A nightly CronJob that reaches a model hub at 02:00 is a
dependency nobody sees until egress is blocked — and then retrieval fails open to
silence and the deployment looks healthy while quietly shipping uncited
advisories. A build-time download is a build failing; a runtime one is a grower
losing a feature without anyone being told.

**Two hooks, pointing opposite ways.** The migration is `pre-upgrade` because
code expecting a column the database lacks is broken code serving traffic. The
corpus ingest is `post-upgrade` and does *not* gate the release, because a missing
corpus is not an outage — the advisory is produced exactly as it was in phase 14.

**No `VINEA_RAG_ENABLED`.** Retrieval is on when the corpus is in the database and
off when it is not. A second switch would let the two disagree, and "it says
enabled, why no citations?" is a worse question than having one source of truth.

## The trap

**Four, and the first two are the same mistake at different layers.**

**1. Measuring the bug instead of the thing.** The first recall run said
lexical-only scored **0.33** and appeared to refute the decision table's claim
that lexical would be the stronger half for a technical manual. It was measuring
a defect. `plainto_tsquery` joins every lexeme with `&`, so the nine-word question

> readily available water RAW and the management allowed depletion fraction p

required all nine terms in one chunk and matched **0 of 798**. The lexical half
was contributing nothing at all while looking, from the outside, exactly like a
working retriever — hybrid still scored 0.92 because dense carried it alone.

Re-joining the lexemes with `|` and letting `ts_rank_cd` rank took lexical-only
from 0.33 to 0.92 and hybrid from 0.92 to 1.00.

The transferable part is not the SQL. It is that **a component can be completely
dead and the system-level metric barely moves**, because the other half
compensates. The measurement that found it was the one that isolated each half —
and the reason to isolate halves is precisely that aggregates hide corpses.

**2. Measuring the chunker instead of the embedder.** Before that, the very first
retrieval probe returned the *licence boilerplate* as the top hit for a
soil-physics query. Not an embedding failure: a crude blank-line chunker had
produced 57 chunks from a 300-page book, and the copyright page was one of the
few things that survived. Fixed by looking at the document's actual structure
instead of assuming it had paragraphs.

Twice in one phase, a "retrieval quality" problem turned out to be a corpus or a
query problem wearing retrieval's clothes. **Suspect the pipeline before the
model.**

**3. A locator that lies.** The first chapter regex was
`^Chapter\s+(\d+)\b\s*[-–—]?\s*(.*)$`, which looks reasonable and matches the
*sentence* "Chapter 1 describes the primary methods for measuring actual ETc".
A prose line became a heading, and every chunk after it was stamped Chapter 1 —
including the entire soil-water-balance chapter. Retrieval still returned the
right passages; they just claimed to be somewhere they were not.

That is the worst failure mode in this phase, and it is worth being precise about
why: a *missing* citation leaves a claim unverified, and a reader knows to be
sceptical. A *wrong* citation moves the claim to falsely verified and sends the
one person who bothered to check to the wrong page, where they find nothing and
conclude the tool is unreliable in general.

The document's real structure, once looked at rather than guessed: the preface
lists chapters as `Chapter 7 - ETc and dual crop ...:`, and the body marks each
start with a bare `Chapter 7`. Titles from the preface, boundaries from the bare
lines.

**4. You cannot bake half a Hugging Face snapshot.** `potion-base-8M` ships its
weights twice — `model.safetensors` (30.2 MB, what the numpy path reads) and
`onnx/model.onnx` (30.2 MB, for a runtime this image does not contain). Fetching
with `ignore_patterns=['onnx/*']` downloads only what is used and halves the
cache, and then loading it offline fails:

```
IncompleteSnapshotError: the cached snapshot ... is incomplete:
3 file(s) are missing (.gitattributes, README.md, onnx/model.onnx)
```

Offline mode verifies that the snapshot is **complete**, not that the files it
needs are present. The guarantee and the pruned cache are mutually exclusive. The
guarantee won, 30 MB was paid, and the attempt is left in the Dockerfile as a
comment — the same treatment phase 13 gave the doc-trimming that saved 0 MB.

## What this phase did *not* do

**Citations record what was *shown*, not what was *used*.** `advisory_citations`
holds the passages retrieval supplied to each leg. Asking the model which sources
it used would be a stronger claim and a *self-report*, and phase 12 exists because
self-report is not evidence — a model can name a citation it never read. So the
UI says "sources shown to the model", and that wording is doing real work.

**Retrieval is not evaluated for whether it changed the advice.** The gate
measures whether the right chapter comes back. Whether a cited passage made the
rationale *better* is a question for an agronomist and phase 12's annotation
queues, not for `recall@3`.

**The gate is twelve hand-written questions.** It is a floor, not a guarantee, and
it discriminates today only because the retrieval is imperfect. If it ever scores
1.00 forever, that means it has stopped measuring.

## Try it

```bash
# 1. The corpus, and the licence check that gates its regeneration.
uv run python scripts/fetch_corpus.py --dry-run
#   licence  : CC BY 4.0   doi: https://doi.org/10.4060/cd6621en
#   chunks   : 798 across 10 chapters, mean 972 chars

# 2. Ingest and ask it something. Needs Postgres WITH pgvector.
docker compose up -d postgres          # pgvector/pgvector:pg16, not postgres:16
uv run alembic upgrade head
python -m vinea.rag ingest
python -m vinea.rag search "crop coefficient Kc for grapes mid-season"
#   [3] Chapter 6 ... Table 6.3 ... "Temperate climate fruit trees, vines and shrubs"

# 3. Watch the lexical half die, the way it did here.
psql "$DATABASE_URL" -c "
  SELECT count(*) FROM corpus_chunks
  WHERE to_tsvector('english', text) @@ plainto_tsquery('english',
        'readily available water RAW and the management allowed depletion fraction p')"
#   0        <- every lexeme ANDed
psql "$DATABASE_URL" -c "
  SELECT count(*) FROM corpus_chunks
  WHERE to_tsvector('english', text) @@ replace(plainto_tsquery('english',
        'readily available water RAW and the management allowed depletion fraction p')::text,
        '&','|')::tsquery"
#   535      <- the fix, in one line

# 4. The recall gate, and the structural invariant it sits beside.
uv run pytest tests/test_rag.py -v

# 5. Prove the core cannot reach retrieval — the test that outlives this phase.
uv run pytest tests/test_rag.py::test_the_protected_core_never_imports_retrieval -v

# 6. The image, with and without.
docker build --target app -t vinea:plain .                        # 391 MB
docker build --target app --build-arg RAG=1 -t vinea:rag .        # 649 MB
docker run --rm --entrypoint python vinea:rag -c \
  "from vinea.rag.embedding import StaticEmbedder; print(len(StaticEmbedder().encode(['x'])[0]))"
#   256      <- with HF_HUB_OFFLINE=1; no network was touched
```
