# ADR-006: Kubernetes, provider-agnostic and on demand

- **Status:** accepted
- **Date:** 2026-07-28
- **Milestone:** phase 13 (containerize & deploy)

## Context

Phases 6–12 built a production architecture that has never been in production.
The queue reaps expired leases, the router has a degraded path, the prompt
registry fails open — each is an answer to a failure that only happens when
something runs unattended, and each is currently supported by a test that stages
the failure rather than by having survived one.

Deploying forces two questions that a laptop never asks. First, this system is not
one process but three, with genuinely different demands: an **API** that wants to
scale to zero, a **Streamlit UI** that holds stateful sessions and does not, and a
**worker** that is not request-driven at all — it wakes at 02:00, drains a queue,
and exits. Second, once there is a *second* deploy, code and schema have to move
past each other without ever being incompatible.

ADR-003 established the rule that governs this decision: *complexity must earn its
place*. It rejected Redis because a second stateful system is a constant
operational cost for a workload that peaks once a night. Applied unchanged to an
orchestrator, that same sentence rejects Kubernetes: a control plane, a node pool,
ingress, secrets machinery and a YAML surface larger than `src/`, to run one
nightly batch and two small web processes.

So this ADR has an argument to defeat, not merely a preference to record.

## Decision

**Kubernetes, expressed as one provider-agnostic Helm chart, deployed on demand
against an ephemeral cluster — with Postgres managed and outside it.**

The standing argument is defeated on three counts:

1. **Every runtime shape gets a native primitive.** `Deployment` for the API,
   `Deployment` with session affinity for the UI, `CronJob` for the worker. No
   managed runtime handles the third without a second resource kind bolted
   alongside, because a queue-draining batch process is not a request.

2. **Migration-on-deploy gets a first-class mechanism.** A Helm `pre-upgrade` hook
   Job runs `alembic upgrade head` to completion before any new pod receives
   traffic. That ordering lives in the chart, declaratively, rather than as three
   steps in a CI script that a future edit can reorder. And because rollouts are
   real, the expand/contract window is *demonstrable* — old and new pods serving
   against one schema, which is the thing the lesson needs to show and which
   revision-based platforms hide.

3. **It reduces vendor lock-in rather than adding it.** One chart runs on GKE,
   AKS, EKS, k3s or a laptop. This is a correction: the first draft of the phase
   document listed "a cloud dependency" as a cost of the managed-runtime option
   and then failed to credit Kubernetes for removing it.

And the count that actually decides it: **ADR-003 was optimising operating cost
for a small workload. This repository's objective function is teaching.** A reader
who finishes phase 12 and deploys to a proprietary runtime learns that runtime; one
who deploys to Kubernetes learns the substrate underneath most production systems.
Changing the objective is what makes this a defeat of the argument rather than an
exception to it.

**Free control plane is not a free cluster.** GKE gives one zonal control plane
free, AKS gives a free tier with no uptime SLA, EKS charges ~$0.10/hour — and in
every case the nodes, storage and egress are the actual bill. Since this build must
cost nothing to run, the verified target is a real `kind` cluster started on demand:
locally, and in GitHub Actions on every push. It is a conformant Kubernetes
cluster; what it is not is permanent.

## Alternatives considered

**A managed runtime (Cloud Run, Fly.io, App Runner).** Genuinely simpler, and for
the API alone it is the better answer — scale to zero, no cluster, a URL in one
command. Rejected on the shape mismatch, which is concrete rather than
philosophical: the worker is not request-driven, so it becomes a second resource
kind (a Job plus a scheduler) with its own mental model; and Streamlit needs a
pinned warm instance, so the platform's headline feature is switched off for the
component that most looks like a web app. Add that every manifest is then written
in one vendor's dialect, and the simplicity is smaller than it appears.

**Fly.io specifically**, which was the closest call. Long-lived machines fit the
worker and Streamlit better than anything else considered. Rejected because its
Postgres has historically been *unmanaged* — an app you run, where you are the DBA
for backup, failover and upgrade. That is precisely the second stateful system
ADR-003 refused. Pairing Fly compute with an external managed Postgres removes the
objection and also removes most of the reason to pick Fly.

**Kubernetes on a paid managed cluster (AKS Free tier or GKE zonal + one node).**
Not rejected — *deferred*. The chart runs there unchanged, and `infra/` documents
it as the step to take when a permanent URL is wanted. It is not the primary target
only because it costs money to keep alive, and a reader who cannot follow along
learns nothing.

**EKS.** The same manifests, plus a per-cluster hourly charge on top of the nodes.
Rejected as the most expensive way to run an identical artifact.

**Postgres in-cluster, as a StatefulSet.** Tempting, because it would make the
whole deployment self-contained and genuinely free. Rejected on ADR-001: the
advisories and their provenance are the one thing in this system that cannot be
recomputed, and this would move them onto the newest and least-proven component,
with backup, failover and version upgrades becoming ours again. The standing
argument is untouched here and this ADR does not try to touch it.

**External Secrets Operator + a cloud secret manager**, instead of Sealed Secrets.
The better answer at a company: rotation without a commit, and one place to audit
access. Rejected here because it requires a cloud provider and a billing account,
which is the one thing this phase cannot assume. Sealed Secrets costs nothing and
runs on `kind`.

**A single VM running `docker compose up`.** Nearly what `docker-compose.yml`
already describes, and the cheapest thing that could work. Rejected because it
teaches nothing this phase exists to teach: with no rolling deploy there is never a
window where old and new code share a schema, so expand/contract — the actual
lesson — never appears. It also returns Postgres, TLS and restarts to us by hand.

## Consequences

**Good.**

- The three runtime shapes stop being an awkwardness to route around and become
  three resource kinds in one chart.
- The deploy path is **exercised on every push**, not diagrammed. CI starts a real
  cluster, installs the chart, runs the migration hook, rolls out, and smoke-tests
  through the API. On a paid cluster this would be a nightly job someone mutes.
- `helm rollback` gives the code half of the rollback story a first-class verb,
  which matters because the schema half deliberately has none (see below).
- The artifact is portable. Moving to a paid cluster is a values file, not a
  rewrite.

**Costs, accepted.**

- A YAML surface, for an application whose source is a few thousand lines. This is
  exactly what ADR-003 warned about; what changed is not the cost but what is
  bought with it.
- **Nothing stays up.** There is no permanent public URL at the end of this phase.
  That is the price of "free", stated plainly rather than implied.
- Free-tier managed Postgres scales to zero, so the first query after idle pays a
  wake-up. `/health` will report that honestly rather than mask it.
- The provider-specific glue — ingress class, storage class, IAM — is the one part
  CI does not verify, because `kind` does not have it. The OpenTofu module under
  `infra/tofu/` is `fmt`-checked and `validate`d in CI, and never applied.
  **Not** `plan`ned: a plan queries provider APIs, so it needs live credentials
  and a billing account, and a step that cannot run is not a gate — it is a step
  that always passes. Saying "planned in CI" would have been the same unexercised
  confidence this phase exists to remove, one level up.

**The one worth understanding.** Kubernetes makes rolling deploys easy, and a
rolling deploy is what creates the problem this phase is really about. Two
replicas of the old code and one of the new all query **one** schema
simultaneously. So the schema must be compatible with both, which forces
expand/contract: add nullable, backfill and dual-write, and only drop the old
column in a *later* deploy, once no pod reads it.

The asymmetry is the trap. Rolling code back is `helm rollback`. Rolling a schema
back is usually impossible: Alembic's `downgrade()` is a development tool, and a
downgrade that drops a column does not restore the data it held — it destroys it.
Hence the rule this phase adopts: **migrations are forward-only in production, and
the rollback strategy is to roll the code back to a version that still works
against the current schema.** Expand/contract is not a ceremony around that rule;
it is what makes the rule possible.

## Verification

```bash
# A real cluster, locally, free:
kind create cluster --name vinea
helm upgrade --install vinea ./infra/chart --wait

# The hook ordering is the claim -- the migration Job must reach Completed
# before any new pod is Ready:
kubectl get jobs -l app.kubernetes.io/component=migrate
kubectl rollout status deploy/vinea-api

# What CI asserts on every push:
uv run pytest tests/test_deploy.py -q    # skips without a cluster, never fails red
```
