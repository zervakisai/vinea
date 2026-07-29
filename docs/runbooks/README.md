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

| runbook | fires on | urgency |
|---|---|---|
| [advisory-missing.md](advisory-missing.md) | a tenant has no advisory by 06:00 local | page |
| [degraded-rate.md](degraded-rate.md) | >5% degraded over 7 days | ticket, never a page |
| [queue-not-draining.md](queue-not-draining.md) | queue depth rising through the night | page |

## Why there are only three

Every one of these corresponds to an SLO. Alerts that do not map to a promise
tend to be symptoms — CPU, memory, error counts — and paging on a symptom trains
people to acknowledge pages without reading them. If a fourth alert is proposed,
the first question is which promise it protects.
