# Runbooks

One per alert, and the rule is simple: **if an alert ships, its runbook ships in
the same commit, or the alert does not ship.** An alert without a runbook is a
page that begins with someone reading source code at 03:00.

Each runbook answers four questions in the same order, because that is the order
a person woken by a page needs them in:

1. **What is actually broken** — in terms of the promise, not the metric.
2. **What to check first** — the one query or command that distinguishes the
   likely causes.
3. **What to do** — including the option of doing nothing.
4. **What this costs** — against the error budget, so the decision to wait is a
   priced decision rather than a hopeful one.

| runbook | fires on | urgency | payload flag |
|---|---|---|---|
| [advisory-missing.md](advisory-missing.md) | a tenant has no advisory by 06:00 local | page | `urgent: true` |
| [degraded-rate.md](degraded-rate.md) | >5% degraded over 7 days | ticket, never a page | `urgent: false` |
| [queue-not-draining.md](queue-not-draining.md) | queue depth rising through the night | page | `urgent: true` |

## How you find out

`python -m vinea.slo check` runs at 06:05 local. It records the breach in
`slo_breaches`, exits non-zero, and — if `VINEA_ALERT_WEBHOOK_URL` is set — posts
the breaches to it as one JSON message carrying the measured value, the target, the
error budget and a link to the runbook below.

The `urgent` flag above is the machine-readable form of the urgency column, so a
Slack workflow or an ntfy priority can route the degraded-rate notice somewhere
quieter without re-deriving which objective is allowed to interrupt a person.

Two things it deliberately does not do. It does not deduplicate: a breach that
persists notifies once each morning, because going quiet after the first message
makes an unresolved breach indistinguishable from a resolved one. And it cannot
fail the check — a webhook returning 500 is logged and ignored, or a red
`slo check` would come to mean *"Slack might be down"*.

## Why there are only three

Every one of these corresponds to an SLO. Alerts that do not map to a promise
tend to be symptoms — CPU, memory, error counts — and paging on a symptom trains
people to acknowledge pages without reading them. If a fourth alert is proposed,
the first question is which promise it protects.
