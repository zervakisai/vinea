# Phase 18 — SLOs, and the debts the previous phases wrote

`git checkout phase-18`

> The problem statement and decision table below were written **before** the
> build. Everything from *Open questions — answered* onward was written after —
> including one debt paid, one paid in part, and one recorded as permanent.

## What you learn

That an SLO is not a number you pick — it is a number you can *measure*, about a
promise someone actually made, with an error budget that changes what you do when
it is spent. And that most systems already contain a deadline that looks like an
SLO and is not one.

## The problem

### 1. There is a deadline in the schema, and it is the wrong deadline

Phase 8 built this, correctly, and named it carefully:

```python
# How long a whole day's advisory may take across all its worker attempts, from
# first enqueue. ONE deadline, never extended (S3.3).
DEFAULT_DEADLINE_SECONDS = 1800
```

`advisory_tasks.deadline_at` is set at enqueue and never moved. It is a genuine
control and it answers a genuine question — *how long should we keep retrying
before giving up?* — which is a question about **the worker's patience**.

The SLO is a different question: *when does the grower need this?* A vineyard
manager plans the day's irrigation and spraying before work starts. The promise
is "an advisory for tomorrow, available by 06:00 local time". That deadline is a
wall-clock time on a calendar day; the existing one is a duration from an
arbitrary enqueue instant.

They are close enough to be confused and far enough apart to matter. Enqueue at
02:00 and 30 minutes lands at 02:30, comfortably inside the promise. Enqueue at
05:50 — because the scheduler was late, which is exactly when you care — and the
task gives up at 06:20 having *never violated its deadline*, while the grower has
no advisory at the moment they needed one.

**A deadline that cannot be missed while the promise is broken is not an SLO.**

### 2. Three phases wrote debts into their own docs

Each was recorded honestly at the time and none has been paid:

| phase | debt | consequence |
|---|---|---|
| 13 | Langfuse is not deployed (ADR-004's stack is Postgres + ClickHouse + Redis + MinIO) | `trace_id` is NULL in the cluster — *"the deployed system starts out less observable than the laptop"* |
| 14 | `advisories.model_id` records the gateway alias `vinea-nightly` | one of B2's five drift tags no longer resolves to a concrete model without the gateway's own logs |
| 16 | tokens are counted only behind a gateway | `input_tokens` and `context_chars` are NULL on a laptop, so the calibration built in phase 16 has never been fed |

The pattern is worth naming: **every one of these is a measurement that exists in
development and vanishes in production.** That is the exact inversion of what an
operations phase is for.

### 3. Nothing is aggregated, and nothing alerts

`queue_depth_samples` is charted. `advisories.degraded` is a column. `eval_runs`
has scores. Not one of them is compared against a target, and no threshold
anywhere produces a notification. The system can be asked how it is doing; it
cannot say when it is doing badly.

### 4. A dashboard is not a runbook

Phase 11's operator panel shows queue depth over time — the signal DESIGN.md's B1
says you autoscale on. It says nothing about what to *do* when the line goes up.
An alert without a runbook is a page that begins with someone reading source code
at 03:00.

## The three SLOs, and why these three

| SLO | target | why this and not something else |
|---|---|---|
| **Advisory availability** | ≥ 99% of tenant-days have an advisory by 06:00 local | This is the product. Everything else in the system exists to make it true |
| **Read latency** | p95 of `GET /advisories/…` < 300 ms | The grower-facing read. Deliberately not the *write* path: `POST` enqueues and returns 202, so its latency is a queue property, not a user-visible one |
| **Judgement rate** | < 5% of advisories `degraded` over 7 days | The one that measures whether the *interesting* half of the system is working. A fleet quietly running on the deterministic path for a week is correct, useful, and not the product that was built |

The third is the one worth defending. `degraded=true` is not an error — phase 8
built that path on purpose and a grower gets real physics from it. So it can
never page. But a month of it means the model layer has been effectively absent
and nobody noticed, which is why it is an SLO and not an alert.

## Error budget arithmetic, stated before it is convenient

99% over 30 days, one advisory per tenant per day: **0.3 missed advisories per
tenant per month.** With ten tenants that is three misses a month across the
fleet — roughly one bad night per ten days before the budget is gone.

That number is small enough to be uncomfortable, and saying so now is the point.
An error budget chosen after the first breach is a number chosen to excuse the
breach.

What spending it *means* has to be decided in advance too, or it means nothing:

- **Budget remaining:** ship. Phase 12's eval gate and phase 17's audit gate are
  the only things standing between a change and production.
- **Budget spent:** stop shipping changes that touch the nightly path until the
  cause is understood and the budget recovers.

## Decision table

| Question | Options | Verdict |
|---|---|---|
| **Where do SLIs come from?** | a metrics backend (Prometheus) · SQL over the tables we already have | **SQL first** — every SLI here is a question about rows that already exist: did an advisory land before 06:00, was it degraded, when was it created. A metrics pipeline is the right way to serve them *continuously*; it is the wrong way to *define* them, because a counter can only measure what someone remembered to increment |
| **`/metrics` for Prometheus?** | yes, full instrumentation · yes, a small hand-rolled exposition · no | **no** — nothing in this deployment scrapes, and a format that assumes a collector which does not exist is a format nobody reads. `/ops/slo` serves the same numbers as JSON through the API, like every other panel (ADR-005). When someone is on a rota, these SLIs become the recording rules and none of the work is wasted |
| **Where does the 06:00 deadline live?** | a constant · a column on `grower_config` | **`grower_config`** — a vineyard in Chile and one in Nemea do not share a morning. The tenant record already carries `region`, and phase 6's argument ("new crop = an INSERT, not a PR") applies unchanged to "this tenant needs it by 05:00" |
| **Alerting** | Alertmanager · a scheduled job that writes a row · nothing, dashboards only | **nothing yet, and runbooks anyway** — a nightly evaluator can only notice a 06:00 breach at 06:00, and Alertmanager is a second stateful system for a rota that does not exist. The runbooks ship regardless, because they are the part that is useful the first time something breaks, with or without a page |
| **Does Langfuse get deployed?** | yes, repay phase 13's debt in full · no, and say why in the ADR | **no — recorded as permanent (ADR-010)** — ADR-004's stack is Postgres + ClickHouse + Redis + MinIO, which repeats ADR-003's argument against a second stateful system four times over. `trace_id` stays NULL in the cluster and the deployed system stays less observable than the laptop. What replaces it is the SLIs: they answer *"is the promise being kept?"* without answering *"what did this run do?"*, and only the first has an SLO |

## Open questions for the build — answered

1. **Can this repository measure its own SLOs?** No, and the machinery is built
   anyway. There is no production traffic; every advisory in the database was
   written by a test. The queries, the targets and the budget arithmetic are real
   and unit-tested against seeded rows; the *numbers* are about a synthetic fleet.
   Stated here rather than left for a reader to infer from a confident-looking
   dashboard.

2. **What is the 06:00 SLI computed from?** `advisories.created_at AT TIME ZONE
   grower_config.timezone`, compared against 06:00 on the day *after* `run_date` —
   an advisory for run_date N advises about N+1 and is read on the morning of N+1.
   The timezone column is new (migration `b4e0d75c1f28`): `region` was already
   there and is a data-residency fact, not a clock.

   The denominator turned out to be the important part. It comes from
   `grower_config`, one row per tenant per day the tenant had an open config —
   because counting only days that *produced* an advisory scores **100%** on a
   night the scheduler never fired. The failure would remove rows from both sides
   of the fraction and make the number better.

3. **Does read latency need instrumentation?** It needs it and does not have it,
   and the SLI returns `None` rather than a number. `Objective.is_met(None)` is
   `None`, not `True`, so nothing downstream can round an absent indicator up into
   a pass. Writing down an objective you are not yet measuring is fine; pretending
   to measure it is not.

4. **What happened to the phase-14 alias debt?** Paid in part, in about five
   lines: `MeteredModel` now records `ModelResponse.model_name` — what the
   provider says served the call — alongside the tokens. The honest limit is that
   what LiteLLM echoes depends on its configuration, so this may still be the
   alias. The mechanism exists and its failure is now visible in a column.

   As for why it sat for four phases: because it was written down as a
   consequence in an ADR, which is where consequences go to be agreed and then
   not acted on. A debt in a table with no owner and no date is a debt nobody
   owes.

## What must not happen

**No SLO gets a target it cannot be measured against.** A number in a document
with no query behind it is a wish.

**No alert without a runbook.** If an alert ships in this phase, the runbook ships
in the same commit, or the alert does not ship.

**No dashboard that implies precision it does not have.** Phase 16's cost panel
already carries a warning that its avoided-spend figure is a counterfactual; the
same discipline applies to an SLI computed over twelve seeded rows.

## Decisions

**ADR-010.** SLOs in SQL; no metrics backend, no alerting daemon, no scrape
endpoint nothing scrapes. Runbooks in the repository, one per alert.

**`grower_config.timezone`,** nullable, defaulting to UTC *on read* rather than as
a `server_default`. A tenant with no recorded zone is judged against a UTC
morning — stricter than their own for anyone east of Greenwich — so the gap shows
up as a breach and sends someone to fix the config. Excluding them would improve
the number and hide the problem.

**An unmeasured objective reports `None`, never a pass.** Read latency is
declared, agreed, and not collected. `Objective.is_met(None)` returns `None`.

**The error budget policy is in the code**, on `ErrorBudget.policy`, so "what do
we do now" is returned by the same call that says the budget is spent rather than
being remembered from a meeting.

**Three alerts, three runbooks, and the degraded-rate one never pages.** Nothing
is broken for a grower when it breaches; waking someone for a correct answer is
how a rota learns to ignore its pager.

## The trap

**The deadline that cannot be missed.** `advisory_tasks.deadline_at` is right
there, it is called a deadline, it is already enforced, and using it as the SLI
would have been one line. It measures the worker's patience — enqueue plus 30
minutes — and a late scheduler produces a task that gives up at 06:20 having never
violated it, next to a grower who had nothing at 06:00.

The general shape: **a system that has run for a while usually contains a
deadline, and it is usually the wrong one**, because it was written to bound work
rather than to keep a promise.

**The denominator that improves when things break.** Computing availability over
the advisories that exist scores 100% on a night the scheduler never fired — the
failure removes rows from both sides of the fraction. The denominator has to come
from `grower_config`, and `test_a_night_that_never_ran_scores_zero_not_one_hundred`
is the assertion that separates the two queries.

**And a timezone test that was wrong in the way timezones are always wrong.** The
first version asserted that 05:30 UTC was on time in London and late in Athens.
London is BST in July, so 05:30 UTC is 06:30 local and *both* were late. The query
was right; the test data was not. Left in the file as a comment, because it is the
exact reason the column stores an IANA zone name and not an offset.

## What this phase settled, and what it did not

**Paid:** phase 14's alias debt, in about five lines — `MeteredModel` records
`ModelResponse.model_name` beside the tokens. Limited by what LiteLLM chooses to
echo, and now visible in a column rather than invisible in a design.

**Recorded as permanent:** phase 13's Langfuse debt. ADR-004's stack is Postgres +
ClickHouse + Redis + MinIO — ADR-003's argument against a second stateful system,
four times over. `trace_id` stays NULL in the cluster. Deciding *not* to pay a
debt, in an ADR, is a legitimate outcome; leaving it as a to-do that never comes
due is not.

**Unpaid by construction:** phase 16's calibration. It needs paired
`context_chars` and `input_tokens` from a metered request, and no provider key
ships with this repository. `python -m vinea.context --calibrate` says so.

**And the honest limit on everything above:** there is no production traffic here.
Every advisory in this database was written by a test. The queries, the targets
and the arithmetic are real; the numbers are about a synthetic fleet.

## Try it

```bash
# 1. The three objectives, measured.
curl -s localhost:8099/ops/slo -H "X-Ops-Key: $VINEA_OPS_KEY" | jq '.[] | {key, value, met, sample_size}'
#   read_latency_p95: value null, met null  <- declared, not collected

# 2. The boundary, from both sides. One minute either way of 06:00 Athens.
uv run pytest tests/test_slo.py -v -k "local_morning"

# 3. The denominator that matters: a night that never ran.
uv run pytest tests/test_slo.py -v -k never_ran

# 4. The arithmetic nobody enjoys.
uv run python -c "
from vinea.slo import error_budget
from vinea.slo.objectives import AVAILABILITY, SLIResult
b = error_budget(SLIResult(objective=AVAILABILITY, value=29/30, sample_size=30))
print(f'allowed {b.allowed_failures}, observed {b.observed_failures}, exhausted {b.exhausted}')
print(b.policy)"
#   allowed 0.3, observed 1, exhausted True

# 5. The runbooks. Read one before you need it.
ls docs/runbooks/
```

## The invariant

```bash
git diff --ignore-blank-lines phase-17 phase-18 -- \
  src/vinea/features.py src/vinea/contracts.py src/vinea/deps.py \
  src/vinea/graph.py src/vinea/reconcile.py src/vinea/pipeline.py     # must be empty
```

The last time this command is run in this sequence. If it is still empty at
phase 18, the claim the whole project was arranged to demonstrate holds: the
physics and the topology written in phases 1–4 never had to change, through
persistence, a second source, a queue, observability, an API, a UI, a prompt
registry, an eval gate, containers, Kubernetes, a gateway, retrieval, context
budgets, row-level security and an operations layer.
