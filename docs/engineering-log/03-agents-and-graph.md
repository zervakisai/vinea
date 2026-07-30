# Phase 3 — Three agents on a graph

`git checkout phase-03`

## What you learn

How to compose several agents so that the *shape* of the composition enforces the
boundary you drew in phase 2 — and the difference between an agent that summarises
two other agents and one that genuinely reconciles them.

## The central idea

The graph is:

```
FeatureBuilderNode (DETERMINISTIC — no model)
  → IrrigationNode (LLM) → SprayNode (LLM) → CoordinatorNode (LLM) → DailyFarmAdvisory
```

The first node calls no model. That is the entire argument made structural: the
deterministic step is a **node in the topology**, visible in the node list and in
the generated mermaid diagram, not a helper function someone might route around.

Three ways to compose agents were available, and the choice is the interesting
part:

| Approach | Why not |
|---|---|
| **Agent-as-tool** | The model decides control flow, and the coordinator's reconcile disappears inside a tool call. Ordering becomes non-deterministic and untestable. |
| **Programmatic hand-off** | The valid runner-up. Works fine, but the deterministic/LLM boundary is just a line of Python, not structure. |
| **Graph** ✅ | Fixed, testable ordering; one typed shared state; the boundary is a node. |

## Decisions

- **`FarmState` vs `Deps`.** State is per-run mutable data (rows, then advices
  filling in as the graph progresses). Deps is injected read-only crop config.
  Conflating them is how "a new crop" becomes a code change.
- **I/O stays at the edge.** `load_weather` runs *before* the graph. The graph's
  first node is already pure computation over rows in memory.
- **The model is bound at run time**, not at construction: `model=config.MODEL` is
  passed to `agent.run`. This is what makes per-call model routing (phase 8) a
  routing decision rather than a rebuild.
- **`instructions`, not `system_prompt`.** Dynamic `@agent.instructions` runs per
  request and reads `RunContext[Deps]`, so the day's numbers and the crop config
  frame the call freshly. `system_prompt` would carry the framing into multi-turn
  history, which is not wanted here.
- **The Coordinator reconciles.** It receives deterministic *conflict facts* from
  `reconcile.py` — rain-fastness against the irrigation plan, spray windows
  against `daylight_bounds` derived from GHI — and must sequence the day and
  populate `conflicts_resolved`. Concatenating the two sub-advices is exactly the
  failure this node is shaped to prevent.

## Grounding guards

`retries=2` on each agent, and one `@agent.output_validator` each. They raise
`ModelRetry`, which re-asks the model with the failure message:

- **Irrigation** must echo `current_depletion_mm` — a drift beyond 0.5 mm is a
  retry — and may not recommend a depth past the field-capacity cap.
- **Spray** windows must be a **subset** of the deterministic candidates. The model
  may narrow a window; it may not invent one.
- **Coordinator** must embed both sub-advices unchanged.
- All three must produce the right `target_date`.
- Confidence is **clamped** to `1.0 − confidence_penalty` (phase 4), so no agent
  can claim certainty the data does not support.

## Read this

- `src/vinea/graph.py` — the topology, and the docstring arguing for it
- `src/vinea/agents.py` — the three agents, the instructions seams, the validators
- `src/vinea/reconcile.py` — `daylight_bounds`, `build_conflict_facts`

## The trap

Irrigation and Spray are **independent** — neither reads the other's output — but
they are chained sequentially here, so you pay two round-trips where one would do.
`asyncio.gather` inside a single node is the fix, and it is deliberately not
applied: this is a nightly batch, nobody is waiting, and the sequential form reads
more clearly.

That is a legitimate trade, but only because the workload is throughput-bound. Put
this same graph behind an interactive "ask now" endpoint and the sequential chain
becomes the wrong answer. Know which one you are building.

## Try it

```bash
uv run pytest tests/test_agents.py -v
```

The whole agent suite runs with **no live model** — `TestModel` and `FunctionModel`
via `Agent.override`, with `ALLOW_MODEL_REQUESTS = False` as a hard backstop in
`conftest.py`.

Read `test_spray_validator_rejects_invented_window` and then try to make it pass
wrongly: point the invented window at `01:00` instead of `12:00`. It stops
failing — because on this data 01:00 *is* inside a real candidate window. A guard
test that asserts rejection is only meaningful if the thing it offers is genuinely
rejectable, which is worth checking whenever the underlying data changes.
