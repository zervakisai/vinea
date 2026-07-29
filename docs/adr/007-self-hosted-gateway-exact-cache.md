# ADR-007: A self-hosted LLM gateway, with an exact-match cache

- **Status:** accepted
- **Date:** 2026-07-29
- **Milestone:** phase 14 (LLM gateway & cost)

## Context

The system has five drift tags, an asymmetric eval gate, a golden replay and a
queue-depth chart, and cannot answer what a night cost. That gap is not the
embarrassing part. This is:

```python
DEFAULT_TENANT_CALL_BUDGET = 100   # jobs/tenancy.py
```

A budget of 100 *calls* does not bound spend, because calls are not fungible. A
200-token call and a 200,000-token call decrement it identically. Widen the
retrieved context (phase 15 will), lengthen an instruction template through the
registry (phase 12 made that a non-deploy operation, deliberately), and spend
moves by an order of magnitude while the counter reports 40/100 and everyone
relaxes. It is also an in-memory tally on a `frozen` dataclass: it resets when a
worker process starts, and two workers draining the same queue each get their
own 100.

A control that reports 40/100 while spend triples is worse than no control,
because people trust it. That is what needs fixing; cost visibility is what makes
the fix checkable.

Three smaller problems arrive with it. One provider is a single point of failure
(`config.MODEL` names one `provider:model`, and a bad night at Anthropic silently
removes the judgement layer from the whole fleet). Nothing caches the model call
— `feature_cache` caches the cheap deterministic half. And phase 8's
`BORDERLINE_FRACTION_OF_RAW`, described there as "a cost/quality dial disguised
as a threshold", has never had a visible cost half.

## Decision

**Run LiteLLM ourselves, in front of every model call, with exact-match response
caching and per-tenant spend ceilings enforced on virtual keys. Store what a
call cost as columns on `advisories`, read from the gateway's response headers.
Reject semantic caching.**

Four consequences worth stating as decisions in their own right:

1. **Cost is evidence, not a derived value.** `input_tokens`, `output_tokens`,
   `cost_usd` and `cache_hit` become nullable columns beside the five drift tags.
2. **The budget moves out of our process and into the gateway.** A LiteLLM
   virtual key with `max_budget` per tenant is a ceiling that survives a restart
   and is shared by every worker.
3. **Down and saying-no degrade in opposite directions.** A gateway *outage*
   falls toward the model (the direct provider); a *budget refusal* falls away
   from it (the deterministic advisory).
4. **No gateway configured means nothing changes.** `resolve_model()` returns the
   plain `config.MODEL` string, and not even its type differs from phase 13.

## Rationale

### Why cost is stored rather than computed

ADR-001 asks one question: could this be recomputed from surviving inputs? The
tokens could. The *price in force the night the call was made* could not.
Providers reprice, and `tokens × price_today` computed in March against a January
advisory returns a number that was never charged to anyone. Worse, the alternative
obliges us to maintain a price table per model per provider — a table that is
wrong silently, on a schedule set by someone else's pricing page.

So the gateway, which knows what it was charged, reports it, and we store it. The
same rule that keeps `raw_mm` out of `grower_config` keeps `cost_per_token` out of
`advisories`: it is `cost_usd / (input_tokens + output_tokens)`, and a derived
value with its own column gets a chance to disagree with itself.

### Why the budget belongs in the gateway

A ceiling in worker memory is a rule in code: it holds while the process lives.
A ceiling on a virtual key is a rule in the one system that sees every call and
persists the tally. This project has now made that trade three times — the unique
index behind advisory idempotency, the partial index behind one-open-config-per-
block, and this. *A rule in code is a promise; in the database it is a guarantee.*

### Why semantic caching is rejected — and why the usual defence does not save it

The standard rebuttal is to make the cache key carry everything that could
invalidate a hit: model version, `prompt_version`, `deps_hash`. That is
**necessary and not sufficient**, and the distinction is the whole decision:

- A weak key causes **staleness** — an answer generated under a prompt or model
  that has since changed. A better key fixes this.
- Similarity matching causes **wrong answers on correct inputs** — two genuinely
  different situations, correctly described, judged near-identical by an embedding
  with no idea that one is above a threshold and the other below. **No key fixes
  this, because nothing in the key is wrong.**

`jobs/tenancy.py` already had the concrete case: *a depletion of 67.4 mm and
67.6 mm sit a hair apart in any embedding space and on opposite sides of the
irrigation trigger.* Semantic caching is rejected on the second ground, not the
first.

Exact-match caching survives the same test easily, and for a reason worth naming:
its failure mode is a wasted API call, never a wrong answer.

### Why the cache is `type: local` and not Redis

ADR-003's standing argument — no second stateful system unless it wins the
argument in an ADR — is not defeated by a cache whose miss is always safe. What
in-process caching buys is real (a re-run night, a retried task) and what it does
not buy is stated plainly: nothing survives a gateway restart, and a second
replica has its own cache. Both are acceptable for a nightly batch. Neither would
be for an interactive product, and that is the line to watch, not the setting.

Consequently the gateway runs **one replica with `strategy: Recreate`**. Two
replicas would not be a capacity win; they would be two half-warm caches and two
spend counters racing on the same key.

### Why LiteLLM's own database, in the same instance

LiteLLM needs persistence for spend. Pointing it at a second Postgres *instance*
would break ADR-003; pointing it at our *database* would put its Prisma migrations
in Alembic's way. Same instance, separate database, and a note in the config that
LiteLLM speaks `postgresql://` while the app speaks `postgresql+psycopg://` —
same server, two dialects, and pasting one into the other's variable fails in a
way that reads like a network problem.

## Rejected alternatives

**A hosted gateway (OpenRouter, Portkey, Helicone).** Zero infrastructure, better
dashboards than we will build, and per-key budgets out of the box. Rejected on the
ADR-004 precedent: that ADR self-hosts Langfuse because sending grower content to
a vendor "is a harder sell than a store we operate ourselves in an EU region". A
hosted LLM gateway sees **every prompt and every completion** — strictly more than
Langfuse would have seen with `include_content=False`. Refusing there and
accepting here would be incoherent. If the EU-residency constraint is ever lifted,
this is the first decision to revisit, and it would be a cheap reversal.

**pydantic-ai's `FallbackModel` alone.** The serious runner-up: it solves the
single-provider problem with no new infrastructure, in an SDK we already depend
on. Rejected as *insufficient* rather than wrong — no spend tracking, no ceiling
in currency, no response cache. It is **folded in** as the rung the gateway itself
falls back to.

**Our own wrapper around the provider SDKs.** Full control, no new service.
Rejected because it means writing and maintaining a price table per model per
provider, a cache, a budget ledger and a failover chain, all of which move when
someone else changes their pricing page, and none of which is this project's
subject.

**Keeping the call budget.** Honest option, zero work. Rejected because problem
one makes it worse than nothing.

**The Anthropic Batch API (50% cheaper).** Deferred, not dismissed. Two obstacles,
one of them ours: pydantic-ai has no batch abstraction, so it would mean leaving
the SDK for one path; and batch is a *latency* trade, with a stated SLA measured
in hours. Phase 18 is about to write down "advisory by 06:00 for ≥99% of tenants",
and a path that cannot promise 06:00 cannot serve it. Revisit when there is a
backfill workload, where latency genuinely does not matter — that is the shape
batch is for.

## Consequences

**We accept:**

- A new service to operate, and a new dependency in the hot path of every model
  call. Mitigated by the failure semantics above, and by the gateway being
  entirely optional.
- **Alias indirection weakens one drift tag.** `advisories.model_id` records
  `vinea-nightly`, not `anthropic/claude-sonnet-4-5`. Resolving an alias back to a
  concrete model now requires the gateway's own logs. That is a real cost of the
  indirection, and phase 18's observability work owes it a fix.
- **The failover rung costs a credential.** `FallbackModel` can only reach the
  direct provider if the pod holds a provider key — which is precisely what
  virtual keys exist to avoid. The chart makes this an explicit choice
  (`gateway.enabled` plus whether a provider key is in the app Secret) rather than
  an accident, and the operator picks: *survives a gateway outage* or *only one
  bounded key to leak*.
- **Budget refusals are recognised by matching text in an error body.** LiteLLM
  answers "out of budget" and "going too fast" with the same status code in some
  versions, and only the body separates them. The fragility is bounded by which
  way it fails — an unrecognised refusal falls over to the direct provider, i.e.
  degrades toward spending money — which is exactly why the gateway *also*
  enforces the ceiling itself. Our check decides how we react; it is not the
  control.

**We get:**

- A spend ceiling that survives a restart and is shared across workers.
- An answer to "what did last night cost, and which tenant cost the most", joined
  to the advisory rather than living in a vendor dashboard.
- The cost half of phase 8's routing dial, visible for the first time.
- Provider failover at two levels, with the gateway's level requiring no key in
  the application at all.

## Revisit if

- The EU-residency constraint changes — a hosted gateway becomes the cheaper
  answer immediately.
- The cache hit rate turns out to matter across restarts, which would be an
  argument for durable caching and a new ADR against ADR-003, not a quiet edit.
- A backfill workload appears, at which point the Batch API's latency trade stops
  costing anything.
