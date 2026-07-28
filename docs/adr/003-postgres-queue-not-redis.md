# ADR-003: A Postgres queue with SKIP LOCKED, not Redis or Celery

- **Status:** accepted
- **Date:** 2026-07-20
- **Milestone:** phase 8 (batch & queue)

## Context

The advisory runs overnight, once per grower, while the grower sleeps (DESIGN.md
B1). That's a throughput problem, not a latency one -- nobody is watching a
spinner -- so the shape is a batch queue: enqueue one job per (tenant, run_date),
have a pool of workers drain it, retry the failures.

The reflex is to reach for Redis + Celery, or SQS, or RabbitMQ. Each is a new
piece of infrastructure to run, monitor, back up, and secure -- and this system
already runs exactly one piece of stateful infrastructure it cannot avoid:
Postgres, because ADR-001 says the advisories and their provenance must be stored.
The question is whether the queue justifies a *second* one.

## Decision

**The queue is a Postgres table, `advisory_tasks`, and workers pull from it with
`SELECT ... FOR UPDATE SKIP LOCKED`.**

The claim query is the whole mechanism:

```sql
SELECT * FROM advisory_tasks
WHERE status = 'queued' AND run_after <= now()
ORDER BY run_after, id
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

`FOR UPDATE` takes a row lock. `SKIP LOCKED` tells a concurrent transaction to
step over any row already locked rather than block on it. So N workers running
this exact query each pull a *different* task, with no external broker, no
coordination protocol, and no possibility of two workers claiming the same row.
The claim commits immediately (status -> 'running'), so the exclusivity survives
the lock being released.

Everything the workers need is columns, because the workers are stateless:

- `status`, `attempts`, `max_attempts` -- retry accounting.
- `run_after` -- backoff; a re-enqueued task with a future `run_after` is simply
  not yet eligible, enforced by the same predicate the partial claim index is
  built on.
- `deadline_at` -- one deadline per day, never extended (S3.3).
- `locked_by`, `locked_at` -- the lease, so a reaper can detect a task whose
  worker died and return it to the queue with no coordination.

Because it's all on the row, recovery is trivial: any process can reap expired
leases, any worker can pick up where a dead one left off, and the whole pipeline
is re-runnable because the task's natural key `(tenant, run_date)` is the *same*
key the advisory UPSERTs on.

## Alternatives considered

**Redis + Celery.** The default choice, and genuinely good at high-throughput,
low-latency task distribution. Rejected here for three reasons. First, it's a
second stateful system to operate for a workload that peaks once a night -- the
operational cost is constant, the benefit is intermittent. Second, Celery's
result/state store would duplicate state we're *already* required to persist in
Postgres, and now two systems have an opinion about whether a job ran. Third, and
most concrete: the thing we most want -- "this advisory, and the task that
produced it, and its provenance, in one query" -- is a JOIN when the queue is a
table and a distributed-tracing exercise when it isn't.

**SQS / a cloud queue.** Removes the operational burden of Redis but adds a cloud
dependency and moves task state off the EU-resident Postgres that ADR-001/ADR-004
deliberately keep grower data on. Same duplicated-state problem. Reasonable at a
scale this system is nowhere near.

**A Python `multiprocessing` pool with no persistent queue.** Simplest, and wrong:
a crash loses the in-flight work with no record of what was running, and there's
no way for a second machine to help. The whole point of persisting task state is
surviving the worker.

**LISTEN/NOTIFY instead of polling.** A real option *on top of* this design --
Postgres can wake a worker when a task arrives, avoiding poll latency. Deferred,
not rejected: the nightly cadence makes poll latency irrelevant (the scheduler
enqueues a batch and the workers drain it), and NOTIFY adds a
connection-management wrinkle that buys nothing until latency matters. The table +
SKIP LOCKED is the substrate either way.

## Consequences

**Good.**

- Zero new infrastructure. The one database the system must run is the queue.
- SKIP LOCKED is a genuinely elegant fit and the lesson of the stage: N workers,
  one query, no broker, no races. The two-connection test asserts the second
  claimer skips the locked row rather than blocking, and `run_worker` drains a
  three-task queue 3/3 with no double-processing.
- Task and advisory share the `(tenant, run_date)` key, so idempotent enqueue and
  idempotent advisory writes compose: the whole night is re-runnable, and a
  scheduler that double-fires or an operator who re-runs by hand can't create
  duplicates.
- Retry lives in exactly one place (S3.3). The SDK owns network/validation
  retries with jitter; the worker owns only the decision to give up on a *day* and
  re-enqueue with backoff. Nothing above the SDK re-issues a request the SDK is
  still retrying -- the double-retry footgun, avoided by construction rather than
  by discipline.
- One deadline per day, computed once and never extended, so a single stuck task
  can't consume the night's budget one retry at a time.

**Costs, accepted.**

- Polling, not push: a worker that finds an empty queue exits (or would sleep and
  re-poll). Fine at nightly cadence; LISTEN/NOTIFY is the upgrade path if latency
  ever matters.
- SKIP LOCKED needs a real Postgres; SQLite can't express it. That's already a
  hard requirement from ADR-001's JSONB and ENUM, so it costs nothing new -- but
  it's why the queue tests skip without a database rather than falling back to a
  lie.
- At very high throughput a dedicated broker would outperform a table. This system
  advises growers overnight; it is orders of magnitude below where that crossover
  lives, and "complexity must earn its place" says don't pay for it now.

## Verification

```bash
docker compose up -d postgres
uv run alembic upgrade head

# End to end by hand (no API key -> the deterministic degraded advisory):
uv run python -m vinea.jobs enqueue --tenant demo --run-date 2026-06-27
uv run python -m vinea.jobs work --max-tasks 1
uv run python -m vinea.jobs status          # done=1

# The tests, including the two-connection SKIP LOCKED assertion:
uv run pytest tests/test_jobs_queue.py -q    # 11 passed
```
