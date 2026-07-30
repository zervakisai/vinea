# Phase 8 — Batch & queue

`git checkout phase-08`

## What you learn

How to turn one advisory into a fleet running overnight without inventing a
distributed system — and the two-line SQL idiom that makes a database into a
perfectly adequate work queue.

## The central idea

[ADR-003](../adr/003-postgres-queue-not-redis.md): Postgres, not Redis.

You already have a transactional database. The queue's claim operation is:

```sql
SELECT ... FOR UPDATE SKIP LOCKED
```

`SKIP LOCKED` is what makes it work: a worker claiming a task locks that row and
concurrent workers *step over* it instead of blocking. N workers drain one queue
with no coordinator, no lock convoy, and — because the claim is in the same
transaction as the state change — no task claimed twice.

Adding Redis would mean a second durability story, a second failure mode, and the
task state living somewhere other than the data it is about. For a nightly batch,
that is a worse system with more moving parts.

## The pieces

| File | Job |
|---|---|
| `scheduler.py` | enqueue one task per `(tenant, run_date)`, **idempotently** |
| `worker.py` | claim with `SKIP LOCKED`, run the work, retry with **one owner** |
| `router.py` | decide from the features alone whether the day needs an LLM at all |
| `degraded.py` | build a complete `DailyFarmAdvisory` with **no model call** |
| `metrics.py` | sample queue depth into the DB so it can be charted |
| `tenancy.py` | per-tenant budgets, and an **exact** feature cache |

## Decisions

- **Idempotent enqueue.** The scheduler keys on `(tenant, run_date)`, so running it
  twice does not double the night's work. Overnight jobs get re-run by operators;
  design for it.
- **One owner per retry.** The retry lives with the worker that claimed the task.
  This is the concrete form of the essay's *double-retry footgun*: pydantic-ai
  already re-asks on validation failure (`retries=2`), so wrapping `agent.run` in
  an outer infra retry multiplies — 2 inner × 3 outer ≈ 6–9 model calls per logical
  request. Own retries in exactly one layer.
- **The router can skip the model entirely.** If depletion has clearly crossed RAW,
  there is no rain, and there are no candidate windows, there is no judgement left
  to make. Generate the explanation and move on. Most nights are like this, which
  is where the cost saving actually comes from.
- **"No model available" degrades to Python, not to failure.** `degraded.py`
  produces a real advisory from the deterministic core. This is only possible
  because of phase 2 — the physics was never the model's job.
- **The feature cache is exact, never similarity.** Keyed on a `deps_hash`. A
  semantic/embedding cache here would add a hop and a false-hit risk to save
  nothing: the keys are small and discrete.

## Read this

- `docs/adr/003-postgres-queue-not-redis.md`
- `src/vinea/jobs/queue.py` — the claim, and the transaction boundary
- `src/vinea/jobs/router.py` — the cheapest possible decision
- `src/vinea/jobs/degraded.py` — an honest advisory with no LLM

## The trap

`BORDERLINE_FRACTION_OF_RAW` decides which days are "clear-cut" enough to skip the
model. That constant is a **cost/quality dial disguised as a threshold**. Widen it
and you save money by having a deterministic path answer questions that deserved
judgement; narrow it and you pay for a model to confirm the obvious.

There is no principled value for it — it depends on how wrong a skipped judgement
is allowed to be, which is a business question. Treat it as one, and make sure the
eval gate (phase 12) scores the routed-away days too. A router that silently
downgrades a third of your advisories while your evals only look at the LLM path is
measuring the wrong population.

## Try it

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run python -m vinea.jobs enqueue --tenant demo --run-date 2026-07-28
uv run python -m vinea.jobs work --max-tasks 1
uv run python -m vinea.jobs status          # done=1
```

Then run `enqueue` twice and confirm the queue depth does not double. Then start two
`work` processes against a full queue and watch neither of them pick up the same
task — that is `SKIP LOCKED` doing the whole job.
