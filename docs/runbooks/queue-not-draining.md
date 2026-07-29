# Runbook: queue depth is rising through the night

**Urgency: page**, because it is the leading indicator of
[advisory-missing](advisory-missing.md). Acting at 03:00 is cheap; acting at
06:30 is too late by definition.

## What is actually broken

Tasks are being enqueued faster than they are completing, or not completing at
all. Nobody has missed an advisory yet — which is the entire reason this alert
exists ahead of the other one.

DESIGN.md's B1 argues you autoscale this fleet on queue depth rather than CPU,
because the workers are I/O-bound on the model API and CPU sits idle right up
until the queue backs up. The same reasoning makes depth the right thing to alert
on.

## Check first

```sql
SELECT sampled_at, queued, running, failed, done
FROM queue_depth_samples ORDER BY sampled_at DESC LIMIT 30;
```

| shape | cause |
|---|---|
| `running` stuck > 0, `queued` flat | a worker died holding a lease |
| `running` = 0, `queued` > 0 | no worker is claiming at all |
| both rising, `done` rising slowly | genuinely too slow — model latency or too few workers |
| `failed` climbing | not a throughput problem; go to [advisory-missing](advisory-missing.md) |

```sql
-- Leases held by a worker that is no longer alive
SELECT tenant, run_date, locked_by, locked_at, attempts
FROM advisory_tasks WHERE status = 'running' ORDER BY locked_at;
```

## What to do

**A dead worker holding leases.** This is what the reaper is for — the lease
state lives on the row precisely so recovery needs no coordination (ADR-003).
Confirm the reaper is running before intervening by hand; a manual reset races it.

**No worker claiming.** The CronJob has finished or never started.

```bash
kubectl get jobs -l app.kubernetes.io/component=worker --sort-by=.metadata.creationTimestamp | tail -3
kubectl logs job/<name> --tail=200
```

A worker that exits immediately is usually a database or migration problem, not a
queue problem — check `/ready` on the API, which reports the same dependency.

**Genuinely too slow.** More workers. `SELECT ... FOR UPDATE SKIP LOCKED` means
concurrent workers never collide, so scaling is adding replicas and nothing else
(ADR-003).

Before scaling, check the *reason* it is slow: if the gateway is retrying against
a struggling provider, more workers make it worse, not better.

**Do nothing** is right if depth is high but falling and the projection clears
06:00. Depth alone is not the alert — depth *not falling* is.

## What this costs

Nothing yet. That is the point of this runbook: the whole value of a leading
indicator is that it can be acted on before any budget is spent.

If it is not cleared before local 06:00, every affected tenant-day spends one
unit of the availability budget — 0.3 per tenant per 30 days — and this becomes
[advisory-missing](advisory-missing.md).
