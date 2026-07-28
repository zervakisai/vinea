# ADR-004: Self-hosted Langfuse, and include_content=False

- **Status:** accepted
- **Date:** 2026-07-20
- **Milestone:** phase 9 (observability)

## Context

DESIGN.md B2 argues that tracing the graph's *own structure* -- a span per node,
FeatureBuilder visibly not an LLM span -- is what separates this system from a
demo. That needs three decisions: where the traces go, whether they carry the
prompt content, and how the graph gets instrumented at all.

The pleasant surprise on the third: it's already done. pydantic-graph emits a span
per node (`auto_instrument`, on by default), and pydantic-ai emits a span per
model call with OTel GenAI attributes when `instrument_all()` is active. Point
both at one provider and the boundary is visible for free -- and **`graph.py` was
not touched** to achieve it, which is the point: observing the boundary didn't
move it.

That leaves the two real decisions: backing store, and content.

## Decision

**Self-host Langfuse as the OTel backing store, and set `include_content=False`.**

Langfuse speaks OTLP/HTTP, so `configure_tracing` points logfire's exporter at
`{LANGFUSE_HOST}/api/public/otel/v1/traces` with basic auth over the
public/secret key pair. logfire owns the global `TracerProvider`, so both
instrumentation paths (graph nodes, agent calls) feed the same tree.

`include_content=False` on the `InstrumentationSettings` keeps span payloads free
of the prompt and response text. The prompts carry the grower's depletion figures,
block location, and yield-adjacent numbers; the trace records *that* a model was
called, its token cost, and which version, but never the grower's numbers.

Tracing degrades to nothing when unconfigured. No `LANGFUSE_*` env -> the exporter
is None, spans are no-ops, `advisories.trace_id` is NULL, and the advisory is
produced exactly as before. Telemetry off is a deployment choice, not an error.

## Alternatives considered

**Logfire (Pydantic's SaaS).** The first-party option, and a genuinely good
product -- it's what pydantic-graph/pydantic-ai instrument *toward* by default.
Rejected for the same reason ADR-001 keeps grower data on a self-run Postgres:
location and yield-adjacent numbers sitting in someone else's multi-tenant cloud
is a harder sell than a store we operate ourselves in an EU region.
`send_to_logfire=False` is set explicitly. (We still *use* the logfire SDK -- it's
the OTel plumbing -- we just don't send anywhere but our own Langfuse.)

**One store for traces, one for evals, one for prompts.** Rejected: Langfuse does
all three (B2's evals in phase 12, B3's prompt registry in phase 12), so self-hosting one
Langfuse means traces, eval runs, and prompt versions live in one system instead
of three, correlated by the same ids.

**include_content=True, scrub later.** Rejected: the safe default for
grower-adjacent data is to never capture it, not to capture it and hope the
scrubber catches every field. Off at the source is one setting; a scrubber is a
maintenance surface that fails open.

**No tracing; rely on the DB provenance columns.** The advisory row already
records model_id, prompt_version, degraded. Rejected as insufficient for the
*structural* question B2 poses: "did the boundary hold, did FeatureBuilder run
before Irrigation, did a node error get papered over" is a shape you read off a
span tree, not a fact a flat row captures.

## Consequences

**Good.**

- The boundary is a picture: a FeatureBuilderNode span with no gen_ai child, next
  to three agent spans that have them. Asserted offline in `test_obs.py` against
  an in-memory exporter -- the tree has the same shape whether the exporter is
  in-memory or Langfuse.
- One store for traces now, evals and prompts later (phase 12), correlated by
  trace/observation ids.
- `advisories.trace_id` (a column since phase 6) deep-links the UI (phase 11) straight to
  the trace. The worker stores it on the large-model path; a test asserts the
  stored id matches the exported trace.
- Grower numbers stay out of the trace by construction.

**Costs, accepted.**

- Langfuse v3 self-host is heavy: its own Postgres, ClickHouse, Redis, and MinIO,
  plus separate web and worker processes. It's behind a `langfuse` compose profile
  so the default `up` stays just the app database, and the app never depends on it
  being present.
- A naive compose misses four real configuration requirements, each a crash or a
  silent drop rather than a warning: `CLICKHOUSE_MIGRATION_URL` (native protocol,
  for migrations); `CLICKHOUSE_CLUSTER_ENABLED=false` (single-node has no
  cluster/Keeper for the `ReplicatedMergeTree` DDL); a MinIO bucket that isn't
  auto-created (spans 500 with "Failed to upload JSON to S3"); and a separate
  `langfuse-worker` (the web accepts spans into a Redis queue, the worker drains
  it to ClickHouse -- without it traces are accepted and never appear). All four
  are in the compose with comments explaining why.
- Batch export means a trace appears a few seconds after the run, not instantly.
  Fine for an overnight system.

## Verification

```bash
# The span tree, offline, no Langfuse needed:
uv run pytest tests/test_obs.py -q               # 7 passed, in-memory exporter

# Live (optional):
docker compose --profile langfuse up -d          # web + worker + datastores + bucket init
# open http://localhost:3000  (login dev@vinea.local / vinea-local-pw)
# set LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY in .env, run an advisory through the
# large-model path, then read the observations back by trace id.
```
