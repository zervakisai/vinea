# Runbook: a tenant has no advisory by 06:00 local

**Urgency: page.** This is the product not existing for someone who planned their
morning around it.

## What is actually broken

A grower opened the app before starting work and there is nothing there. They now
irrigate on yesterday's judgement or their own. That is recoverable — they have
done it for thirty years — but it is exactly the promise this system exists to
keep.

Note what is *not* broken: if `degraded=true` advisories are being produced, the
promise is being kept. The deterministic path is a correct answer, not a failure
(phase 8). Check that before anything else, because it changes the urgency
completely.

## Check first

```sql
-- Which tenant-days are missing, and were they even scheduled?
SELECT g.tenant, d::date AS run_date, a.created_at, t.status, t.attempts, t.last_error
FROM grower_config g
CROSS JOIN generate_series(CURRENT_DATE - 2, CURRENT_DATE, interval '1 day') d
LEFT JOIN advisories a     ON a.tenant = g.tenant AND a.run_date = d::date
LEFT JOIN advisory_tasks t ON t.tenant = g.tenant AND t.run_date = d::date
WHERE g.valid_to IS NULL AND a.id IS NULL
ORDER BY g.tenant, run_date;
```

The `status` column splits the causes cleanly:

| what you see | cause | go to |
|---|---|---|
| no task row at all | the scheduler never fired | *Scheduler did not run* |
| `queued`, `attempts = 0` | the worker never claimed it | [queue-not-draining](queue-not-draining.md) |
| `failed`, `last_error` set | the work itself is broken | *The task failed* |
| `done`, but no advisory | the write failed after the task was marked | *Torn write* — rare, see below |

## What to do

**Scheduler did not run.** The CronJob is `0 2 * * *` in `Europe/Athens`.

```bash
kubectl get cronjob -l app.kubernetes.io/component=worker
kubectl get jobs --sort-by=.metadata.creationTimestamp | tail -5
```

A suspended CronJob or a missed schedule window is the usual answer. Re-run for
the affected date — enqueue is idempotent on `(tenant, run_date)`, so this is
safe even if some tenants already have advisories:

```bash
kubectl run rerun --rm -i --restart=Never --image=<app-image> \
  --env="DATABASE_URL=$DATABASE_URL" \
  --command -- python -m vinea.jobs enqueue --run-date YYYY-MM-DD
```

**The task failed.** Read `last_error` first — it is the exception type and
message, stored on the row precisely so nobody has to find the pod's logs.

```bash
python -m vinea.jobs requeue --tenant <t> --run-date YYYY-MM-DD
```

`requeue` is deliberately an explicit action, not something the scheduler does as
a side effect (phase 8). If it fails again the same way, stop requeuing and read
the error — three identical failures are not a transient.

**Torn write.** `mark_done` and `save_advisory` share one transaction, so this
should be impossible. If you see it, that invariant has been broken and it is a
bug report, not an operational fix.

**Do nothing** is a real option after 06:00. The advisory is for a day that has
started; producing it at 09:00 delivers advice about a morning that is over.
Prefer confirming tonight's run will work.

## What this costs

Each missed tenant-day spends one unit of the availability budget: **0.3 per
tenant per 30 days.** One miss for one tenant exhausts that tenant's month.

If the budget is spent, the policy is not "try harder" — it is **stop shipping
changes that touch the nightly path** until the cause is understood.
