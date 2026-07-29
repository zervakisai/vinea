# The phases

Each phase is one tagged commit and one lesson. `git checkout phase-NN` gives you
the project exactly as it stood there, and `uv run pytest` is green at every tag.

| # | Phase | Tag | Lesson |
|---|---|---|---|
| 1 | Scaffold & ingestion | `phase-01` | [01](01-scaffold-and-ingestion.md) |
| 2 | The deterministic core | `phase-02` | [02](02-deterministic-core.md) |
| 3 | Three agents on a graph | `phase-03` | [03](03-agents-and-graph.md) |
| 4 | Robustness | `phase-04` | [04](04-robustness.md) |
| 5 | The design essay | `phase-05` | [05](05-the-design-essay.md) |
| 6 | Persistence | `phase-06` | [06](06-persistence.md) |
| 7 | A second source | `phase-07` | [07](07-second-source.md) |
| 8 | Batch & queue | `phase-08` | [08](08-batch-and-queue.md) |
| 9 | Observability | `phase-09` | [09](09-observability.md) |
| 10 | API | `phase-10` | [10](10-api.md) |
| 11 | Dashboard | `phase-11` | [11](11-dashboard.md) |
| 12 | Prompt registry & eval gate | `phase-12` | [12](12-prompts-and-evals.md) |
| 13 | Containerize & deploy | `phase-13` | [13](13-containerize-and-deploy.md) |
| 14 | LLM gateway & cost | `phase-14` | [14](14-gateway-and-cost.md) |
| 15 | Retrieval & citations | `phase-15` | [15](15-rag-and-citations.md) |

## How to read this

The phases are not equal in kind. **1–4 build the thing**: ingestion, physics,
agents, robustness. **5 stops and argues** — a design essay written before any of
the rest existed. **6–12 carry that argument out**, one production concern at a
time. **13 onward is what happens after it leaves the laptop**: deployment,
then the operational questions a running system starts asking — what did it cost,
who may spend, and what happens when the thing in front of the model says no.

The single claim the whole sequence is arranged to demonstrate is that the
**physics and the topology** from phases 1–4 never have to change again:

```bash
git diff --ignore-blank-lines phase-04 phase-15 -- \
  src/vinea/features.py src/vinea/contracts.py src/vinea/deps.py \
  src/vinea/graph.py src/vinea/reconcile.py src/vinea/pipeline.py     # empty
```

Note how narrowly that has to be stated to stay true. `ingest.py` and `config.py`
grow (additively — nothing removed). `agents.py` and `cli.py` genuinely change:
phase 12 swaps the instruction f-strings for registry lookups, phase 14 swaps
`model=config.MODEL` for `model=resolve_model()`, phase 15 adds a retrieval call
and a second instruction block, and the CLI gains a `--source` flag. Those are the *wiring* at the edges, not the reasoning.

Every later phase adds a layer *around* the deterministic core rather than
reaching into it. When a phase does force a change, that is worth noticing — and
noticing that two files did change is more instructive than a slogan claiming none
did.

## The shape of each lesson

- **What you learn** — the transferable idea, not the diff.
- **The central idea** — why the phase exists at all.
- **Decisions** — what was chosen, and what was rejected.
- **Read this** — the two or three files that carry the phase.
- **The trap** — the mistake this phase is arranged to avoid.
- **Try it** — something to run or break, to make the idea stick.
