# ADR-010: SLOs measured in SQL, and three debts settled honestly

- **Status:** accepted
- **Date:** 2026-07-29
- **Milestone:** phase 18 (SLOs & operations)

## Context

The system has run for seventeen phases without a promise anyone could check.
`queue_depth_samples` is charted, `advisories.degraded` is a column, `eval_runs`
has scores — and not one of them is compared against a target. The system can be
asked how it is doing; it cannot say when it is doing badly.

It also carries a deadline that looks like an SLO and is not one. Phase 8's
`advisory_tasks.deadline_at` is enqueue-time plus 30 minutes, and phase 8 named it
precisely: how long the worker keeps retrying before giving up. That is a question
about the worker's patience. *When does the grower need this?* is a different
question with a wall-clock answer, and the two disagree in the direction that
matters — enqueue at 05:50 because the scheduler was late, and the task gives up
at 06:20 having never violated its deadline, while the grower had nothing at 06:00.

And three previous phases wrote debts into their own docs, none paid.

## Decision

**Three objectives, defined and measured in SQL over rows the system already
keeps. No metrics backend, no alerting daemon, no scrape endpoint. Runbooks in the
repository, one per alert, shipped in the same commit as the alert.**

| objective | target | window |
|---|---|---|
| advisory available by 06:00 **local** | ≥ 99% of tenant-days | 30 days |
| p95 `GET /advisories/…` | < 300 ms | 7 days |
| advisories from the deterministic path | < 5% | 7 days |

And `grower_config.timezone`, because a vineyard in Nemea and one in Mendoza do
not share a morning.

## Rationale

### Why SQL rather than a metrics pipeline

A counter can only measure what somebody remembered to increment. Every one of
these questions is already answerable from data ADR-001 said could not be
recomputed and therefore had to be stored: when an advisory was created, whether
it was degraded, which tenants had an open config on a given day.

Defining an SLI in SQL means the definition is checkable by anyone with `psql` and
no shared understanding of label cardinality. Serving them continuously is a
different problem, and it is the one a metrics backend actually solves — worth
buying when someone is watching, which brings us to the next section.

### Why the denominator comes from `grower_config`

The single most dangerous way to compute an availability SLI is over the
advisories that exist. A night the scheduler never fired then scores **100%**,
because the failure removes rows from both sides of the fraction. The denominator
is one row per tenant per day the tenant had an open config, and the numerator is
how many of those were delivered on time.

`tests/test_slo.py` seeds a configured tenant with no advisory at all and asserts
0.0, which is the assertion that distinguishes this from the naive query.

### Why the deadline is local, and why an IANA zone rather than an offset

Offsets move twice a year and a stored one is wrong for half of it. The test for
this caught its own author: the first version asserted that 05:30 UTC was on time
in London and late in Athens, which is wrong — London is BST in July, so 05:30 UTC
is 06:30 local and *both* are late. The query was right and the test data was not,
which is exactly the class of error that makes offsets unusable.

`timezone` is nullable and defaults to UTC **on read**, not as a `server_default`.
A tenant with no recorded zone is judged against a UTC morning — stricter than
their own for anyone east of Greenwich — so the gap shows up as a breach and sends
somebody to fix the config. Excluding such tenants would improve the number and
hide the problem.

> **Amended 2026-07-30.** Two of this ADR's stated limits have been closed, and
> the reasoning for the third stands.
>
> **Read latency is now collected.** The section below rejected a process-local
> histogram and a metrics backend, and missed a third option that the traffic
> profile makes available: store every timing. The grower-facing read is served a
> few hundred times a day, so a week is thousands of rows, and at that size an
> exact `percentile_cont(0.95)` in SQL is simpler than bucket boundaries. The
> objective is measured from `api_request_samples`; the property this ADR cared
> about — an idle window reporting *no data* rather than excellent latency — is
> preserved and tested.
>
> **`python -m vinea.slo check` exists**, exits non-zero on a breach, and records a
> row in `slo_breaches`. The rejected alternative below was *alerting from a
> nightly job*, and this is not that: nothing notifies anyone yet — see the
> 2026-07-31 amendment immediately below, which is where that changes. It is the SLO
> equivalent of `alembic check` — one question, one exit code, runnable by a
> person, by cron, or by CI. What the table adds is **duration**: whether we are in
> breach is answerable from the samples, how long we have been is not.
>
> **Amended 2026-07-31 — there is a notification path now.** The paragraph
> below deferred it *"until somebody is on a rota"*, and what changed is not the
> system: one operator watching one channel is a rota of one, and that is the
> honest reason rather than a new technical argument.
>
> `slo check` posts breaches to `VINEA_ALERT_WEBHOOK_URL` when one is set. Unset is
> the default and a supported state — the row is still written and the exit code
> still non-zero, exactly as before.
>
> Three properties it must have, each with a test:
>
> - **A dead webhook cannot fail the check.** An alert path that can turn its own
>   monitor red teaches the operator that a red `slo check` might just mean Slack
>   was down, and the exit code stops carrying information.
> - **Unmeasured never notifies.** `met is None` is not a breach, here for the same
>   reason it is not one for the exit code. A notifier filtering on `not met` would
>   post every morning the latency table was empty.
> - **The URL never reaches a log.** A Slack webhook URL is a bearer credential
>   wearing a URL's clothes, so the failure path logs the exception type and, for an
>   HTTP error, the status — enough to separate *unreachable* from *rejected* from
>   *timed out*, which is every diagnosis it needs to support.
>
> And the *"degraded rate never pages"* rule below moved out of prose and into
> `Objective.pages`. It is still **sent** — the fleet going fully deterministic is
> worth knowing — but flagged `urgent: false` in the payload, so the consumer can
> route it somewhere quieter. Suppressing it entirely would mean the only way to
> learn is to look, which is the problem the notifier exists to solve.
>
> What is still not here: deduplication. A breach that persists notifies once each
> morning. That is intended — the availability window is 30 days, so a breach can
> persist for weeks, and going quiet after the first message makes an unresolved
> breach indistinguishable from a resolved one. Muting is the reader's job; every
> plausible target has a mute button, and none of them can un-drop a message this
> process decided not to send.

### Why read latency is declared and not collected

It is the one SLI with no row behind it. Collecting it means either a middleware
histogram — process-local, dies with the pod, cannot aggregate across replicas —
or a metrics backend this ADR declines to run.

So `read_latency_p95` returns `None` and `Objective.is_met(None)` returns `None`,
not `True`. **An unmeasured objective must never report success**: "no advisories
were late" and "we could not tell whether any were late" look identical on a
dashboard and mean opposite things. A stub returning 0.0 would report a permanent,
excellent, entirely fictional p95.

Writing down an objective you are not yet measuring is not a failure. Pretending
to measure it is.

### Why the error budget is stated before it is convenient

99% over 30 days, one advisory per tenant per day, is **0.3 missed advisories per
tenant per month**. Ten tenants gives three misses across the fleet — one bad
night per ten days before the budget is gone.

That is uncomfortable, and it is what 99% means. An error budget chosen after the
first breach is a number chosen to excuse the breach.

The policy is also decided in advance, or it means nothing: budget remaining →
ship; budget spent → **stop shipping changes that touch the nightly path** until
the cause is understood.

### Why only three alerts

Each maps to a promise. Alerts that map to symptoms — CPU, memory, error counts —
train people to acknowledge pages without reading them. The rule is that a fourth
alert must name the promise it protects.

And the degraded-rate objective **never pages**, because nothing is broken for a
grower when it breaches. Waking someone for a correct answer is how a rota learns
to ignore its pager. That sentence is now `Objective.pages=False` rather than
prose, and the message it produces says `SLO notice` where the other two say
`SLO BREACH`.

## The three debts

**Langfuse is not deployed. Recorded as permanent — and since retracted.**

> **Amended 2026-07-30.** This is no longer true, and the retraction is the
> honest part: "permanent" was a judgement about cost, and the operator decided
> the cost was worth paying. It was never a fact about the system.
>
> Langfuse now runs, verified end to end: `docker compose --profile langfuse up -d`
> exports a real span tree and `tests/test_langfuse_live.py` reads it back and
> asserts that `FeatureBuilderNode` has no model call beneath it — the
> LLM/deterministic boundary observed in the exported trace rather than claimed in
> a docstring. The chart takes `langfuse.host`; keys come from the Secret it
> already reads. See [deploy-langfuse.md](../deploy-langfuse.md).
>
> What stands from the paragraph below: the four-service cost is real, the e2e
> still does not start it, and a cluster deployed *by the e2e* still has
> `trace_id` NULL. What does not stand is calling that permanent.
>
> Worth noting what the debt cost while it was open. Nine phases of code asserted
> that the trace tree shows the boundary, and nobody had ever looked at one. It was
> true — but "recorded as permanent" is how a claim stops being checked.

ADR-004 self-hosts Langfuse, and its stack is Postgres + ClickHouse + Redis +
MinIO. Deploying it repeats ADR-003's argument against a second stateful system
four times over, for a project whose entire thesis has been that complexity must
earn its place. The consequence is real and stays: `trace_id` is NULL in the
cluster, and the deployed system is less observable than the laptop.

This is a debt **documented as permanent rather than paid**, which is a legitimate
outcome and needed saying in an ADR rather than sitting as a to-do that quietly
never comes due. What replaces it: the SLIs above, computed from rows, which
answer *"is the promise being kept?"* without answering *"what did this particular
run do?"*. Those are different questions and only the first has an SLO.

**Phase 14 — the alias weakened a drift tag. Partially paid.**

`advisories.model_id` records `vinea-nightly`. `MeteredModel` now also records
`ModelResponse.model_name` — what the provider says served the call — into the
ledger. Honest limit: what LiteLLM echoes depends on its configuration, so this
may still record the alias. The mechanism is in place and its failure is now
visible in a column rather than invisible in a design.

**Phase 16 — the calibration is wired and empty. Unpaid, by construction.**

`context_chars` and `input_tokens` are written together by `MeteredModel`, which
only wraps the gateway path. No provider key ships with this repository, so
nothing has ever been metered here and `--calibrate` says exactly that. Paying
this debt requires a deployment, not a commit.

## Rejected alternatives

**Prometheus + Alertmanager + Grafana.** The conventional answer, and it is three
more services for a project that argued against one (ADR-003). Revisit when
someone is actually on a rota — at that point the SLIs defined here become the
recording rules, and none of this work is wasted.

**A `/metrics` scrape endpoint without a scraper.** Rejected as furniture. A
format that assumes a collector which does not exist is a format nobody reads.
`/ops/slo` serves the same numbers as JSON, through the API, like every other
panel (ADR-005).

**Using `advisory_tasks.deadline_at` as the SLI.** Free, already there, and
measures the wrong thing. Kept doing its own job.

**Alerting from a nightly job that writes a row.** One table, no new
infrastructure, and it can only notice a breach as often as it runs — which for a
06:00 promise means noticing at 06:00. Deferred rather than rejected: it is the
right first step when someone is on a rota, and pointless before that.

> **Adopted 2026-07-31.** This is what shipped, unchanged in shape: a daily job, a
> row, and now a POST. The limitation stated here is real and accepted — a breach
> at 06:10 is announced at 06:05 the *next* morning. Continuous evaluation is what
> a metrics backend buys, and it is still not worth three services for a rota of
> one. See the amendment at the top of the Rationale.

## Consequences

**We accept:**

- Latency is an objective with no indicator. Visible as `null`, never as a pass.
- The SLIs are computed over a synthetic fleet. There is no production traffic
  here, and every advisory in the database was written by a test. The machinery is
  real; the numbers are about seeded rows, and the phase doc says so.
- ~~Breaches are noticed when someone looks. There is no notification path.~~
  Superseded 2026-07-31. A breach is now announced to `VINEA_ALERT_WEBHOOK_URL` —
  once, on the morning the daily job next runs, which for a breach at 06:10 means
  roughly 24 hours later. That latency is what we accept in place of a metrics
  backend that evaluates continuously.
- `grower_config.timezone` is nullable, so the availability number is only as good
  as the config behind it — deliberately, and it fails toward *pessimism*.

**We get:**

- Three promises with targets, an error budget with arithmetic, and a policy for
  spending it that was written before the first breach.
- Runbooks that answer "what do I do" including "nothing", with the cost of
  waiting priced against the budget.
- A definition of *availability* that cannot score 100% on a night nothing ran.

## Revisit if

- Anyone goes on a rota — then Alertmanager, and these SLIs become the recording
  rules.
- A tenant negotiates a different deadline — `grower_config` already has the
  column; the hour becomes a value beside the timezone.
- The judgement-rate objective breaches for a month — that is a conversation about
  what this product is, not an incident.
