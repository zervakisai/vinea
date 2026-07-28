# Phase 13 — Containerize & deploy

`git checkout phase-13`

> **Status: essay first.** This document's problem statement and decision table
> were written before the Dockerfile existed, the same way `DESIGN.md` preceded
> phases 6–12. The **Decisions**, **The trap** and **Try it** sections are
> completed once the build is done and the claims can be checked by hand.

## What you learn

That "it works on my machine" is not a joke about laziness — it is a precise
statement about which failure modes you have never been exposed to. And that the
moment a system is deployed twice, the interesting question stops being *how do I
ship code* and becomes *how do code and schema move when they cannot move
atomically*.

## The problem

Phases 6–12 built a production architecture that has never been in production.

That sentence is worth sitting with, because most of what those phases built are
answers to failures that only occur when something runs unattended:

- The queue reaps **expired leases** — but no worker has ever died mid-task.
- The router has a **degraded path** for "no model available" — but a key has
  never actually expired at 02:00.
- The prompt registry **fails open** to a bundled floor — but Langfuse has never
  been unreachable during a real request.
- `DataQuality` lowers confidence on a **stale feed** — but the feed has never
  gone stale on its own, only in a fixture.

Every one of those is currently a claim supported by a test that arranges the
failure. Tests are how you check a mechanism; deployment is how you find out
whether you guessed the failure right.

Concretely, today:

| | State |
|---|---|
| Running it | needs a checkout, a Python toolchain, `uv`, and a hand-written `.env` |
| The overnight batch | DESIGN.md B1 is built around a 02:00-local run. Nothing schedules it. `python -m vinea.jobs enqueue` is typed by a human, in the afternoon |
| API & UI | bind to localhost; reachable by exactly one person |
| The database | a container with a volume on one laptop. Its backup story is the laptop's |
| Deploying twice | has never happened, so the migration-on-deploy question has never been asked |

The last row is the one this phase is really about. Containerizing is a morning's
work. Deploying a **second** time, over a schema that already holds a grower's
advisories, is where the actual engineering is.

## Three runtime shapes, one system

The deploy target is not obvious, and the reason is that this system is not one
process. It is three, with genuinely different demands:

| Shape | Command | Traffic | Wants |
|---|---|---|---|
| **API** | `uvicorn vinea.api.main:app` | a handful of reads per grower per day; writes are 202-and-enqueue | scale to zero, fast cold start |
| **UI** | `streamlit run src/vinea/ui/app.py` | one operator, long sessions, stateful WebSocket | a warm instance and session affinity — it does **not** scale to zero gracefully |
| **Worker** | `python -m vinea.jobs work` | nothing. It is not request-driven at all. It wakes at 02:00, drains a queue, exits | a scheduler and a long timeout |

A platform that only knows how to run request-driven containers handles two of
these badly. A platform built around long-lived machines handles the API's
scale-to-zero badly. **This is the whole difficulty of the choice**, and any
option that appears to have no cost here is one whose cost has not been found yet.

Two facts already fixed by earlier ADRs constrain everything below:

- **EU data residency.** ADR-001 and ADR-004 keep grower data on EU-resident
  infrastructure; `grower_config.region` records which region a tenant is bound
  to. The target must offer EU regions for both compute *and* the database.
- **A real Postgres is mandatory.** `SELECT ... FOR UPDATE SKIP LOCKED` (ADR-003),
  JSONB, and native ENUMs (ADR-001) are all load-bearing. There is no SQLite
  fallback and there is not going to be one.

## Kubernetes, and the argument it has to defeat

The standing rule in this repository is ADR-003's: *complexity must earn its
place*, and specifically, no second stateful system for a workload that peaks once
a night. That argument was written against Redis. Applied unchanged to an
orchestrator it says: a control plane, a node pool, ingress, secrets machinery and
a YAML surface larger than `src/` is a lot to carry for one nightly batch and two
small web processes.

Kubernetes is the choice here anyway, because the argument is genuinely defeated
on three counts — and one of them is a correction to this document's own first
draft:

1. **All three runtime shapes get a native primitive.** Deployment, Deployment
   with session affinity, CronJob. The table above showed that no
   request-driven-only platform handles the worker without a second resource kind
   bolted alongside. Here the worker is not an exception; it is a `CronJob`.
2. **Migration-on-deploy gets a first-class mechanism.** A Helm `pre-upgrade` hook
   Job runs to completion before any new pod receives traffic — declaratively,
   rather than as ordering glue in a CI script. And because rollouts are real, the
   expand/contract window is *demonstrable*: you can watch old and new pods serve
   against one schema. On a revision-based platform that window is hidden.
3. **It reduces lock-in rather than adding it.** The first draft of this document
   listed "a cloud dependency" as an accepted cost. That was backwards. One chart
   runs on GKE, AKS, EKS, or a laptop; a managed-runtime deployment runs on
   exactly one vendor.

And the decisive one for *this* repository: ADR-003 was optimising **operating
cost for a small workload**. This project's objective function is **teaching**. A
reader who completes phases 1–12 and then deploys to a proprietary managed runtime
learns that runtime. One who deploys to Kubernetes learns the substrate under most
production systems. That is a different objective, which is what makes it a defeat
of the argument rather than a violation of it.

**What does not change:** Postgres stays *managed and outside the cluster*.
Running Postgres in-cluster would put the one irreplaceable thing (ADR-001) onto
the one component we just added. The standing argument survives intact there, and
this phase does not touch it.

## The finding that shapes everything: free control plane ≠ free cluster

The constraint on this build is that it must be **free to run**. That turns out to
be the most clarifying requirement in the phase, because it exposes something the
pricing pages bury:

| Target | Control plane | Nodes | Actually free? |
|---|---|---|---|
| **GKE** zonal | free — one per billing account, via a monthly credit | you pay | **no** |
| **AKS** Free tier | free, explicitly *no uptime SLA*, positioned for dev/test | you pay | **no** |
| **EKS** | ~$0.10/hour per cluster | you pay | **no** |
| **kind / k3d** | free | free — your machine, or the CI runner | **yes, and it is a real cluster** |

Control-plane fees are a small fraction of a Kubernetes bill; nodes, storage and
egress are the bill. So "free managed Kubernetes" is a category error for a cluster
that stays up. What is free is a *conformant* cluster you start on demand.

*(Free-tier terms move. These were checked while writing this phase and should be
re-checked before anyone relies on them.)*

## The decision table

| Option | What breaks | Verdict |
|---|---|---|
| **Provider-agnostic Helm chart, verified against a real `kind` cluster in CI** | No public URL and nothing stays up — this deploys on demand, not permanently. The chart is exercised end-to-end on every push (install, pre-upgrade migration hook, rollout, smoke test), so what is *unverified* is only the provider-specific glue: ingress class, storage class, IAM | **chosen** |
| **AKS Free tier / GKE zonal + one small node** | The honest paid path, and the chart runs unchanged. Rejected as the *primary* target only because it costs money to keep alive and a reader cannot follow along. Documented in `infra/` as the step to take when a permanent URL is needed | deferred, not rejected |
| **EKS** | Charges per cluster-hour on top of nodes, so it is the most expensive way to run the same manifests | rejected |
| **Managed runtime (Cloud Run / Fly / App Runner)** | Cheaper for the API alone, and genuinely simpler. But the worker is not request-driven, so it needs a second resource kind; Streamlit needs a pinned warm instance; and the whole thing is one vendor. Free tiers exist but scale to zero in ways a stateful Streamlit session dislikes | rejected — see the three counts above |
| **Postgres in-cluster (StatefulSet)** | Would make the deployment fully self-contained and free. Rejected on ADR-001: the advisories are the one thing that cannot be recomputed, and this would put them on the newest, least-proven component in the system, with backup and failover becoming ours again | rejected, standing argument holds |
| **Managed Postgres, free tier, outside the cluster** | Scale-to-zero means a cold first query; free storage is small; the EU region must be selected deliberately. All acceptable — and it must support **pgvector**, because phase 15 depends on it | **chosen** |

## What we are paying, named upfront

- **A YAML surface.** Chart, values, three workload kinds, a hook, an ingress.
  For a system whose `src/` is ~5k lines. This is the cost ADR-003 warned about
  and it is real; what changed is what we are buying with it.
- **Nothing stays up.** The chosen target is on-demand. A permanent public URL is
  a paid step, documented but not taken.
- **A cold first query.** Free-tier Postgres scales to zero. The first request
  after idle pays for the wake-up — which, note, the `/health` endpoint will
  surface honestly rather than hide.
- **Langfuse does not come along.** ADR-004's self-hosted stack is Postgres +
  ClickHouse + Redis + MinIO. That is its own phase. Tracing degrades exactly as
  designed — unset `LANGFUSE_*` means `advisories.trace_id` stays NULL and nothing
  errors — so the deployed system starts out *less* observable than the laptop
  one. Phase 18 has to face this, and the lesson there should not pretend
  otherwise.

## The migration-on-deploy problem

This deserves its own section because it is the part that bites, and it is
invisible until the second deploy.

A deploy moves **two artifacts that cannot move atomically**: the container image
and the database schema. During a rolling deploy, old and new code are both
serving, against **one** schema. So the schema must be simultaneously compatible
with both.

That gives the rule, in three steps that are **three separate deploys**, not one:

| Step | Migration | Safe because |
|---|---|---|
| **Expand** | add the nullable column / the new table | old code ignores it; new code tolerates NULL |
| **Migrate** | backfill; new code dual-writes | both readers see something valid |
| **Contract** | drop the old column | only after *no* running instance still reads it |

The contract step cannot be in the same deploy as the expand step. If it is, there
is a window where the old instance queries a column the migration just dropped.

### Rollback is not symmetric, and this is the trap

Rolling back **code** is easy: redeploy the previous image.

Rolling back **schema** is frequently impossible. Alembic's `downgrade()` is real
and it works and it is a *development* tool. A `downgrade` that drops a column
does not restore the data that column held — it destroys it. A downgrade that
narrows a type discards rows that no longer fit. Once a migration has run in
production and traffic has written through it, its downgrade is a data-loss
operation wearing the costume of an undo button.

So the rule this phase adopts:

> **Migrations are forward-only in production.** The rollback strategy for a bad
> deploy is to roll the *code* back to a version that still works against the
> *current* schema — which is exactly what expand/contract guarantees is possible.

That is not a workaround. It is the reason expand/contract exists.

### Where the migration runs

| Where | Failure mode |
|---|---|
| At container start, in every instance | N instances race. Alembic locks `alembic_version`, so it is not *corrupting* — but startup serializes behind the lock, and the losers can exceed their startup probe and get killed while waiting. The trap is that this works perfectly at N=1 |
| Manually, by an operator | works until the person is asleep, which is when the deploy pipeline runs |
| **A one-shot step in the pipeline, between build and deploy** | the chosen shape: image is built and pushed, a migration Job runs `alembic upgrade head` to completion, and only then does new-revision traffic start |

The GitHub Actions pipeline therefore has an ordering that is not negotiable:
**test → build → migrate → deploy → smoke test.** The migrate step must be
expand-only, and must succeed before any new instance serves.

## Settled, and still open

**Settled:**

- **Packaging is a Helm chart**, for the `pre-upgrade` hook specifically — the
  migration ordering belongs to the chart, not to a CI script that a future
  pipeline edit could reorder.
- **Secrets are Sealed Secrets.** The house rule says secrets never live in
  tracked files; in Kubernetes the manifests *are* tracked, so the rule needs a
  mechanism rather than discipline. Sealed Secrets encrypts to a cluster-held key,
  so the sealed form is safe to commit. It was chosen over External Secrets +
  a cloud secret manager because it costs nothing and needs no provider, which is
  this phase's binding constraint. **Its cost, named:** rotation requires a
  commit, and the controller's private key becomes a thing that must be backed up
  — lose it and every sealed secret in the repo is unrecoverable ciphertext.

**Still open, to be answered by the build rather than assumed:**

1. **Does `config.py` need to read `PORT`?** Preference is to handle the port in
   the image's `CMD`, so `src/vinea/` stays untouched and the invariant below is
   kept rather than repaired.
2. **One image or three?** One image with three entrypoints builds once and
   guarantees the three shapes share a dependency set; three images are each
   smaller. Leaning one — decided on measured size against the <300 MB target,
   noting that `streamlit` + `pandas` are the heaviest things in the tree and the
   worker needs neither.
3. **How does the smoke test authenticate?** `/health` is unauthenticated and
   already checks DB reachability, which makes it the right *liveness* target. But
   a smoke test that only proves a container started is theatre: it must read
   through the API with a real key and assert on a real advisory.
4. **What does Terraform provision, if the cluster is ephemeral?** Under a `kind`
   target there is no cloud cluster to create, and infrastructure code that
   provisions nothing is cargo cult. The honest split: it owns the *paid* path
   (cluster + managed Postgres + registry), lives in `infra/tofu/`, and is
   checked but never applied.

## The invariant

Phase 13 should not touch the deterministic core at all — it is packaging and
infrastructure. This is the first phase where that claim ought to be trivially
true rather than argued:

```bash
git diff --ignore-blank-lines phase-12 phase-13 -- \
  src/vinea/features.py src/vinea/contracts.py src/vinea/deps.py \
  src/vinea/graph.py src/vinea/reconcile.py src/vinea/pipeline.py     # must be empty
```

If the `PORT` question above forces a change to `config.py`, that is additive and
outside the protected set — but it will be recorded here rather than absorbed
silently.

---

*Everything above was written before the build. Everything below was written
after, and three of the four open questions got answers the essay did not
predict.*

## Decisions

- **Two images, not one** — settled by measurement, as promised. The UI stack is
  220 MB (`pyarrow` 119, `pandas` 48, `numpy` 24, `streamlit` 29) and neither the
  API nor the worker imports any of it. Streamlit and pandas became a `ui` extra;
  the `dev` group self-references `vinea[ui,...]` so a bare `uv sync` behaves
  exactly as before.
- **The provider is a build argument.** `VINEA_MODEL` selects one provider at run
  time and the image was carrying five. `--build-arg PROVIDER=anthropic` (the
  default, matching `config.MODEL`) installs one. Each of the five in
  `config._KEY_ENV` is its own extra.
- **`PORT` is read in `CMD`, never in `config.py`** — which is what kept the
  protected core untouched. Question 1 answered without a compromise.
- **`/ready` was added** to `api/main.py`. Question 3 turned out to be the wrong
  question; see The trap.
- **`hook-delete-policy: before-hook-creation`, without `hook-succeeded`.** The
  first draft had both, and a successful migration then deleted its own Job — so
  the e2e's "did the hook run?" assertion found an empty list *exactly when
  everything had worked*. Evidence should outlive success.
- **The worker's `backoffLimit` is 0.** Retry belongs to one layer (ADR-003):
  the task row owns `attempts`/`max_attempts`. A Kubernetes retry on top is the
  double-retry footgun one level further out — k8s restarts the pod, the pod
  re-drains the queue, the night's model budget multiplies.
- **CI runs the same script a human runs.** `infra/kind-e2e.sh`, not a YAML
  re-implementation that nobody executes on a laptop and that drifts until the
  day it matters.

## Read this

- `Dockerfile` — two targets, and the measurements in the comments
- `infra/chart/templates/migrate-job.yaml` — the hook, and why the delete policy
  is one word shorter than the obvious version
- `infra/chart/templates/worker-cronjob.yaml` — `backoffLimit: 0`, and why
- `infra/kind-e2e.sh` — the deploy, executable
- `src/vinea/api/main.py` — `/health` and `/ready`, and the docstrings arguing
  that they answer different questions
- `tests/test_deploy.py` — probe semantics offline; chart assertions skip without
  `helm`

## The trap

**`/health` returns 200 with the database gone, and that is correct.**

The obvious healthcheck is `curl -f /health`. It passes with no database at all,
because the endpoint deliberately reports trouble in its body rather than raising
— so the obvious check is theatre, verifying only that a process is listening.

The fix is not to make `/health` return 503. A liveness probe that fails on an
unreachable database restarts every pod in the deployment, repeatedly, for a
fault no restart can fix — converting a Postgres outage into a cluster-wide crash
loop that is *harder* to recover from than the outage. Liveness must stay 200.

So the two questions need two endpoints: `/health` for "is this process alive?"
and `/ready` for "should this pod receive traffic?", the latter returning 503.
Kubernetes draws exactly that line — readiness removes endpoints, liveness kills
containers — and an `httpGet` probe reads the status code and nothing else, which
is why it had to be a code and not a field.

**The second trap is in this phase's own test suite**, and it is worth more than
the first. Three probe tests passed on the first run, and passed for the wrong
reason: the fixtures were `lambda: iter([session])`, and FastAPI decides how to
treat a dependency with `inspect.isgeneratorfunction`. A lambda returning an
iterator is not a generator function, so FastAPI injected the *iterator object*
as the session; `session.execute(...)` raised `AttributeError`, the route caught
it, and every probe reported "unreachable" — including the one that was supposed
to prove a healthy database returns 200. It only surfaced because that fourth
test failed.

Both traps are the same shape: a green result that is not evidence. It is the
same failure the spray-window test had in phase 3, where an "invented" window was
rejected because *no* windows existed.

**The third one only CI could find.** The e2e script printed `PASS` and exited
non-zero. Its cleanup function was written as a one-liner:

```bash
cleanup() { [[ $CLEANUP -eq 1 ]] && { ...delete cluster...; }; }
```

With `--cleanup` not passed, the test is false, so the `&&` chain returns 1, so
the function returns 1 — and it is the last command of the `EXIT` trap, so **that
becomes the script's exit status**. Every step succeeded and the process reported
failure.

It survived every local run because every local run was piped into `tail`, and a
shell pipeline reports the exit status of its *last* command. `tail` always
succeeds. The bug was invisible from the machine it was written on and obvious to
the first runner that executed it bare.

Three traps in one phase, all the same shape: a green result that is not
evidence. The pattern is worth more than any of them individually — ask what
*could* have made this pass, and check that it did not.

**A fourth, smaller and measured:** you cannot delete your way out of a base
image's size. `RUN find /usr/share/doc /usr/share/man -delete` in a later layer
saved exactly 0 MB — the bytes live in the base layer and a subsequent layer only
adds whiteout entries. The trim was tried, measured, and removed.

## What it cost

| | |
|---|---|
| `app` image | **309 MB** — 3% over the 300 MB target, deliberately |
| `ui` image | far over, and not asked to meet it: `pyarrow` alone is 119 MB |
| The last 48 MB | dropping precompiled bytecode: 261 MB, but application import goes 1.28 s → 2.19 s. Kept the bytecode; an API that scales to zero pays that second on every cold start |
| Chart | 6 resources, one of them a hook |
| Not deployed | Langfuse (ADR-004's stack is Postgres + ClickHouse + Redis + MinIO). Tracing degrades as designed — `trace_id` stays NULL — so the deployed system starts out *less* observable than the laptop. Phase 18 owes this back |

## Try it

```bash
# The whole thing, free, on a real cluster:
./infra/kind-e2e.sh              # add --cleanup to delete the cluster after

# Probe semantics, offline, no cluster and no database:
uv run pytest tests/test_deploy.py -v

# The paid path, checked but never applied:
cd infra/tofu && tofu init -backend=false && tofu validate
```

Then break the ordering on purpose. Add a migration that fails —

```python
def upgrade() -> None:
    op.execute("SELECT 1/0")
```

— and re-run the e2e. `helm upgrade` fails at the hook, the failed Job and its pod
stay behind for you to read (that is `before-hook-creation` doing its job), and
**no new pod ever serves traffic**. That is the guarantee, demonstrated rather
than asserted. Then delete the migration and watch the same command succeed.

Second exercise: change `region` in `infra/tofu/variables.tf` to `us-central1`
and run `tofu validate`. It fails, because EU residency is a promise ADR-001 and
ADR-004 made in prose and this variable turns into a rule the tool enforces.
