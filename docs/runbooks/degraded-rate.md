# Runbook: more than 5% of advisories are degraded

**Urgency: ticket. Never a page.** Nothing is broken for a grower.

## What is actually broken

Nothing, for anyone reading an advisory. `degraded=true` means the advisory came
from the deterministic path — the FAO-56 water balance and the spray gates, in
Python — and phase 8 built that on purpose. A grower gets real physics, honestly
flagged.

What is broken is the *product*. A fleet running deterministically for a month is
a system whose entire judgement layer has been absent while every dashboard stayed
green, and nobody noticed because nothing failed. That is why this is an SLO and
not an alert, and why paging on it would be wrong: waking someone for a correct
answer teaches them to ignore pages.

## Check first

```sql
-- Which route, and since when?
SELECT run_date,
       count(*)                                   AS total,
       count(*) FILTER (WHERE degraded)           AS degraded,
       count(*) FILTER (WHERE model_id IS NULL AND NOT degraded) AS skipped_by_router
FROM advisories
WHERE run_date > CURRENT_DATE - 30
GROUP BY run_date ORDER BY run_date;
```

Three causes, and the third is not a problem:

| pattern | cause |
|---|---|
| step change on one date | a key expired, or the gateway went down and stayed down |
| gradual rise | tenants hitting their spend ceiling one by one |
| high but flat, `degraded = false` | the phase-8 router is skipping clear-cut days. Working as designed |

That last column matters: `skipped_by_router` days are **not** degraded. They are
complete advisories that did not need a model, and they are a cost saving, not a
gap.

## What to do

**No API key / gateway unreachable.** `config.has_api_key` returns False and the
worker takes the degraded path without erroring, by design.

```bash
kubectl get secret vinea-secrets -o jsonpath='{.data}' | jq 'keys'   # names only
kubectl logs -l app.kubernetes.io/component=worker --tail=100 | grep -i degraded
```

**Budget refusals.** The gateway is declining, not failing. That is the control
working; the question is whether the ceiling is right.

```sql
SELECT tenant, count(*) FROM advisories
WHERE degraded AND model_id IS NULL AND run_date > CURRENT_DATE - 7
GROUP BY tenant ORDER BY 2 DESC;
```

Raise the tenant's LiteLLM key budget deliberately, or accept the degradation.
Do not route around it — phase 14 exists because the previous budget was a number
nobody could act on.

**Do nothing** is correct if the router is responsible. Check
`BORDERLINE_FRACTION_OF_RAW` before changing anything: it is a cost/quality dial,
and phase 16's cost panel shows what it saves.

## What this costs

The judgement-rate objective is 5% over 7 days. Unlike availability, spending
this budget costs no grower anything today — it is a signal that the system is
becoming a different, cheaper, less useful product than the one that was built.

Treat a sustained breach as a design conversation, not an incident.
