# Phase 14 — LLM gateway & cost

`git checkout phase-14`

> The problem statement and decision table below were written **before** the
> build. Everything from *Open questions* onward was written after, when the
> claims could be checked — and two of them changed.

## What you learn

That "what did last night cost?" is an architectural question, not a billing one —
and that a budget denominated in the wrong unit is worse than no budget, because
it looks like a control.

## The problem

Four gaps, and the second is the one worth stopping at.

**1. One provider is a single point of failure.** `config.MODEL` names one
`provider:model` string. If Anthropic is down at 02:00, every tenant falls to the
deterministic degraded path. That degrade is *correct* — phase 8 built it on
purpose, and a grower gets real advice from real physics — but it means one
vendor's bad night silently removes the judgement layer from the entire fleet,
and the only trace is `degraded=true` on every row.

**2. The budget exists and counts the wrong thing.** This is not a missing
feature; it is a misleading one:

```python
DEFAULT_TENANT_CALL_BUDGET = 100   # jobs/tenancy.py
```

A budget of 100 *calls* does not bound spend, because calls are not fungible. A
200-token call and a 200,000-token call both decrement it by one. Widen the
retrieved context (phase 15 will), lengthen an instruction template through the
registry (phase 12 made that a non-deploy operation, deliberately), and spend
moves by an order of magnitude while the budget reports 40/100 used and everyone
relaxes. It is also an in-memory tally on a `frozen` dataclass, so it resets
every time a worker process starts — the ceiling is per-run, not per-night, and
two workers draining the same queue each get their own 100.

**3. Nothing caches the model call.** `feature_cache` caches the deterministic
features (phase 8), which is the cheap half. The expensive half — the model call
itself — runs every night for every tenant, including tenants whose weather,
crop config, and prompt have not moved since yesterday.

**4. Cost per advisory is unknown.** The system carries five drift tags, an
asymmetric eval gate, a golden replay and a queue-depth chart, and cannot answer
what a night cost, which tenant cost the most, or whether the phase-8 router's
`BORDERLINE_FRACTION_OF_RAW` is saving anything. That constant was called out in
phase 8 as "a cost/quality dial disguised as a threshold" — and there has been no
way to see the cost half of the dial.

## Where the gateway goes, and why nothing upstream changes

The seam already exists, and phase 3 put it there for a different reason:

```python
res = await irrigation_agent.run(..., deps=deps, model=config.MODEL)
```

The model is bound **at run time**, not at construction. So pointing
`VINEA_MODEL` at a gateway is a configuration change. `agents.py` does not learn
that a gateway exists, and the protected core certainly does not.

That is the same shape as ADR-002's `WeatherSource`: a seam drawn for one reason
that pays out later for another.

> **Written before the build, and half wrong.** *Routing* really is pure
> configuration — LiteLLM is OpenAI-compatible, so `VINEA_MODEL=openai:vinea-nightly`
> plus `OPENAI_BASE_URL` would reach it with no code at all. But *failover* and
> *metering* both need a `Model` object rather than a string, and the only place to
> hand one over is the call site. So `agents.py` did change, in exactly three
> lines: `model=config.MODEL` → `model=resolve_model()`.
>
> Small, and worth being precise about rather than rounding down to zero. The
> protected six are untouched, `agents.py` is wiring at the edge and has changed
> before, and the honest statement is "three lines in the wiring", not "nothing".

## The decision table

| Option | What breaks | Verdict |
|---|---|---|
| **LiteLLM, self-hosted, in front of every call** | A new service to run. Its budget/spend tracking needs a database — but it can use the Postgres we already run, so ADR-003's "no second stateful system" survives. Adds a hop to every model call, and becomes a single point of failure *for the LLM path* unless it fails open to the direct provider | **chosen** |
| **A hosted gateway (OpenRouter, Portkey, Helicone)** | Zero infrastructure, better dashboards than we will build. Rejected on the ADR-004 precedent: that ADR self-hosts Langfuse because sending grower content to a vendor "is a harder sell than a store we operate ourselves in an EU region" — and a hosted LLM gateway sees **every prompt and every completion**, which is strictly more than Langfuse would have seen with `include_content=False`. Refusing there and accepting here would be incoherent | rejected |
| **pydantic-ai's `FallbackModel` alone** | The serious runner-up, and it solves problem 1 with no new infrastructure at all — it is already in the SDK we depend on. Rejected as *insufficient*, not wrong: it gives failover and nothing else. No spend tracking, no per-tenant ceiling in currency, no response cache. It stays in the design as the thing the gateway falls back to when the gateway itself is unreachable | folded in |
| **Our own wrapper around the provider SDKs** | Full control, no new service. Rejected because it means writing and maintaining a price table per model per provider, a cache, a budget ledger and a failover chain — all of which move when providers change pricing, and none of which is this project's subject | rejected |
| **Do nothing; keep the call budget** | Honest option. Rejected because problem 2 makes it worse than nothing: a control that reports 40/100 while spend triples is a control people trust | rejected |

## Cost is evidence, not a derived value

The instinct is to compute cost when someone asks: tokens are on the trace, the
price list is a constant, multiply. That is wrong for the same reason ADR-001
gives about features versus advisories.

**Prices move.** A `cost = tokens × price_today` computed in March against a
January advisory returns a number that was never charged to anyone. The tokens
are reproducible; the *price at the moment of the call* is not, and neither is
which model version served it.

So cost joins the provenance columns on `advisories`, alongside the five drift
tags, as columns that are nullable because they arrive later and empty is honest:

| Column | Why it cannot be recomputed |
|---|---|
| `input_tokens`, `output_tokens` | Reproducible in principle, stored because the cost is meaningless without them |
| `cost_usd` | What was actually charged, at the price in force that night |
| `cache_hit` | Whether this advisory cost anything at all |

And `raw_mm`'s rule still applies inside the row: cost-per-token is **not** a
column, because it is `cost_usd / (input_tokens + output_tokens)`.

## Caching: exact yes, semantic no — and the key is not the fix

`jobs/tenancy.py` already contains the argument, and it is stronger than the one
usually given:

> a depletion of 67.4 mm and 67.6 mm sit a hair apart in any embedding space and
> on opposite sides of the irrigation trigger

The standard defence of semantic caching is to make the key carry everything that
could invalidate a hit — model version, `prompt_version`, `deps_hash`. That is
**necessary and not sufficient**, and the distinction is the lesson:

- A weak key causes **staleness**: you serve an answer generated under a prompt or
  model that has since changed. The key fixes this.
- Similarity matching causes **wrong answers on correct inputs**: two genuinely
  different situations, correctly described, judged near-identical by an embedding
  that has no idea one is above a threshold and the other below. No key fixes
  this, because nothing in the key is wrong.

So: exact-match caching, on a deterministic fingerprint, the way `feature_cache`
already works. Semantic caching stays rejected, and ADR-007 records that it is
rejected on the second ground, not the first.

## The expand migration — phase 13's mechanism, first real use

Adding four columns to `advisories` is the first schema change since the
migration hook was built, and it is deliberately a *good* case: additive,
nullable, no backfill. The expand/contract rule says this is one deploy, safely:

| | |
|---|---|
| Old code + new schema | ignores four columns it does not select. Fine |
| New code + new schema | writes them. Fine |
| Rollback to old image | old code runs against the new schema unharmed — which is the whole point of expand-only |

If this phase needed to *rename* or *narrow* a column, it would be three deploys
and the lesson would be longer. It does not, and saying so is the honest version
of "the mechanism works": the easy case proves the plumbing, not the hard case.

## Open questions for the build — and what they turned out to be

**1. Does LiteLLM share the app's Postgres or get its own database?** Same
instance, separate database. Same instance keeps ADR-003 intact; a separate
database keeps LiteLLM's Prisma migrations and our Alembic migrations from ever
meeting. One detail only shows up when you do it: LiteLLM wants
`postgresql://`, the app wants `postgresql+psycopg://`. Same server, two
dialects, and pasting one into the other's variable fails in a way that reads
like a network problem.

**2. Fail open to what, exactly?** The question was better than the answer I had
written down. "Fail open to the direct provider" is right for an *outage* and
catastrophically wrong for a *budget refusal*, and both arrive at the same call
site as an exception. Falling over to the provider when the gateway declines
would route around the control the phase exists to build. So the ladder leans two
ways, and `gateway/budget.py` is the whole file that decides which:

| The gateway is… | Degrade toward | Because |
|---|---|---|
| unreachable (connect, timeout, 5xx) | the **direct provider** | nothing about the request was wrong; the grower keeps a judged advisory and we pay list price for one night |
| refusing on budget (400/429 + "budget") | the **deterministic advisory** | the request was understood and denied; retrying burns the night's attempts against an answer that will not change until a human raises a limit |

**3. Where does cost get read?** From `x-litellm-response-cost` on the HTTP
response — which means the seam that can read it is the **httpx client we
inject**, not the SDK's typed `ModelResponse`. That is correct of the SDK: a
vendor's out-of-band metadata has no business in a provider-agnostic response
type. Two mechanisms therefore, and they divide cleanly: tokens come from
`ModelResponse.usage` and are available always; cost and cache-hit come from
headers and are available only behind a gateway. Which is why a no-gateway night
records tokens-or-nothing and an honest NULL for money.

**4. Do Anthropic batch submissions still report per-request cost?** Moot, and
that is the more useful answer. Batch trades **latency** for 50%, with an SLA
measured in hours; phase 18 is about to write "advisory by 06:00 for ≥99% of
tenants". A path that cannot promise 06:00 cannot serve that SLO, so the cost
attribution question never gets asked. Deferred to a backfill workload, where
latency genuinely does not matter — which is the shape batch is for. ADR-007
records it as deferred rather than rejected.

## What actually got built

`resolve_model()` is the whole application-side surface, and the seam it uses was
drawn in phase 3 for a different reason — the model is bound at **run time**, so
importing `agents.py` needs no API key. Three rungs:

| Configuration | `resolve_model()` returns | Gateway outage degrades to |
|---|---|---|
| no `VINEA_GATEWAY_URL` | the plain `config.MODEL` **string** | n/a — nothing changed |
| gateway, no provider key | `MeteredModel(gateway)` | the deterministic advisory |
| gateway + provider key | `MeteredModel(FallbackModel(gateway, direct))` | the direct provider |

The first row is the one to notice. Not a wrapper, not an object — the *type* does
not change. That is not tidiness: a `str` stays lazy under
`Agent.override(model=TestModel())`, so the offline suite keeps running with no
API key at all. Return an eagerly-built Model there and every test in this
repository needs an `ANTHROPIC_API_KEY` to reach the line that ignores it.

And rung three is not free, which the essay had not noticed. The reason to run a
gateway with virtual keys is that the deployment never holds a provider key — a
leaked virtual key spends its budget and no more. Keeping a provider key beside
it for failover puts the unbounded credential back in the pod. The choice is
real, it is the operator's, and the chart makes it explicit rather than letting
it depend on a key someone left in a Secret for an unrelated reason:

> **survives a gateway outage**  ↔  **only one bounded key to leak**

## The four columns, and one that is deliberately absent

`input_tokens`, `output_tokens`, `cost_usd`, `cache_hit` — nullable, no
`server_default`, beside the five drift tags. A default would make every advisory
written before tonight claim it cost zero, a fabricated number in the one place
the phase exists to make trustworthy.

`cache_hit` is `all()`, not `any()`: an advisory is three model calls, and two
cached out of three still bought a completion. The column answers "did this
advisory cost anything new?", and it is read by someone deciding whether the
cache is worth keeping.

`cost_per_token` is **not** a column. It is `cost_usd / (input_tokens +
output_tokens)` — the same rule that keeps `raw_mm` out of `grower_config`, applied
inside a row.

## Decisions

**ADR-007.** Self-hosted LiteLLM; exact-match caching; semantic caching rejected
on the *wrong-answers* ground rather than the staleness one; hosted gateways
rejected on the ADR-004 precedent; the Batch API deferred on the SLO.

**The budget moved out of our process.** `DEFAULT_TENANT_CALL_BUDGET` stays in
`jobs/tenancy.py`, superseded and annotated, as the counter-example. The ceiling
now lives on a LiteLLM virtual key: denominated in money, persistent across
restarts, shared between workers. Third time this project has made that trade —
after the unique index behind advisory idempotency and the partial index behind
one-open-config-per-block. *A rule in code is a promise; in the database it is a
guarantee.*

**Two Secrets, not one.** Provider keys and `LITELLM_MASTER_KEY` live in
`vinea-gateway-secrets`, which only the gateway pod reads. App pods read
`vinea-secrets` and hold a virtual key with a ceiling. Collapsing them would hand
every workload the unbounded credential and undo the reason for the gateway — so
the chart asserts the separation in a test rather than trusting values.

**One gateway replica, `strategy: Recreate`.** LiteLLM's cache is `type: local`,
in-process, chosen over Redis because ADR-003's standing argument is not defeated
by a cache whose miss is always safe. Two replicas would not be capacity; they
would be two half-warm caches and two spend counters racing.

**One config file, two deployments.** `infra/chart/files/litellm-config.yaml` is
mounted by compose and served as a ConfigMap by the chart. It lives under the
chart because Helm's `.Files.Get` cannot reach outside it — and one file means a
local run and a cluster install cannot disagree about which model serves
`vinea-nightly`, which is the drift that makes "it worked locally" a real
sentence.

## The trap

**Three, and the first is the one that would have shipped.**

**1. The failover that routes around the control.** `FallbackModel`'s default
`fallback_on` is `ModelAPIError`. A budget refusal is a `ModelHTTPError`, which is
a `ModelAPIError`. So the default behaviour is: gateway declines to spend more →
SDK tries the direct provider → provider obliges → money spent, ceiling silently
not a ceiling. It would have passed every test that existed, because the advisory
would have been produced correctly. The failure is invisible until the invoice.

The fix is a predicate, `should_fall_back`, and a test with a tripwire model that
fails the run if the direct provider is ever reached through a refusal. The
general shape is worth more than the fix: **a fallback path inherits none of the
reasoning of the path it replaces.** Every degrade in this system now has to
answer "toward what, and does that undo something?"

**2. The predicate that got too wide.** Behind a gateway the deployment may hold
no provider key at all, so `has_api_key()` had to stop meaning "is
`ANTHROPIC_API_KEY` set" — otherwise every night degrades with a healthy model one
hop away. Widening it took ten seconds and immediately broke rung three:
`resolve_model()` asked the same function whether a *direct* key existed, got
`True` because a gateway URL was set, and tried to build an `AnthropicModel` with
no key. It failed loudly, which was luck rather than design.

Two questions had been sharing one function because the answers used to coincide:

```python
has_api_key(model)        # can we call a model at all?      -> gateway OR provider
has_provider_key(model)   # can we call the provider direct? -> provider only
```

The tell is that `has_api_key` needed a *new* caller with a *different* meaning.
That is usually the moment to split, not to widen.

**3. A measurement that rotted with no commit.** Phase 13 recorded the `app`
image at 309 MB. Rebuilt from that same tag today, on the same machine, it is
**389 MB** — and not one line of this repository changed. `ARG PYTHON_VERSION=3.12`
resolves to `python:3.12-slim-bookworm`, a floating tag, and the base moved
underneath the number. Phase 13's Dockerfile pins uv by exact version with a
comment about how floating toolchains cause "it built yesterday", three lines
above a base image pinned by name only.

The base is not pinned by digest here, deliberately, and phase 13's number is left
as written with a dated correction beside it. A measurement is a fact about a
moment; rewriting it would hide the lesson, which is that **a number in a document
has no test protecting it.** Every measured claim in this repository is a claim
about a build that may no longer exist.

Phase 14's own numbers, so they can rot legibly too: `app` **389 MB** unchanged by
this phase, and **419 MB** with `--build-arg GATEWAY=1` — the OpenAI-wire SDK
costs 30 MB, and it is needed even when the upstream is Anthropic, because LiteLLM
speaks the OpenAI wire format.

## What this phase did *not* fix

**Alias indirection weakened a drift tag.** `advisories.model_id` now records
`vinea-nightly`, not `anthropic/claude-sonnet-4-5`. That indirection is the point
of a gateway — swapping the upstream is not a deploy — but resolving an alias back
to a concrete model a month later now requires the gateway's own logs, which no
part of this system reads. B2's five drift tags are down to four and a pointer.
Phase 18 owes it, next to the observability debt phase 13 left.

**The expand migration proved the easy case.** Four additive nullable columns with
no backfill: one deploy, safe in every direction. A rename or a narrowing is three
deploys and a different lesson. Saying so is the honest version of "the mechanism
works".

## Try it

```bash
# 1. No gateway: nothing changes, and the type says so.
uv run python -c "from vinea.gateway import resolve_model; print(repr(resolve_model()))"
# 'anthropic:claude-sonnet-4-5'   <- a str, resolved lazily

# 2. Point at a gateway and watch the ladder pick a rung.
VINEA_GATEWAY_URL=http://localhost:4000 uv run python -c "
from vinea.gateway import resolve_model
m = resolve_model(); print(type(m).__name__, '->', type(m.wrapped).__name__)"
# MeteredModel -> OpenAIChatModel      (rung 2: no provider key, no failover)
# MeteredModel -> FallbackModel        (rung 3: ANTHROPIC_API_KEY also set)

# 3. Run a real gateway. Needs a provider key; nothing else in the stack changes.
docker compose --profile gateway up -d
curl -s localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY" | jq '.data[].id'

# 4. The expand migration, and the nullability that is the actual claim.
uv run alembic upgrade head
psql "$DATABASE_URL" -c "\d advisories" | grep -E 'input_tokens|output_tokens|cost_usd|cache_hit'
#   ... integer  |  |          <- nullable, no default. NULL means "nobody knew".

# 5. The whole phase, offline.
uv run pytest tests/test_gateway.py -v

# 6. The chart in all three gateway modes.
helm template vinea infra/chart | grep -c VINEA_GATEWAY_URL                       # 0
helm template vinea infra/chart --set gateway.enabled=true | grep -c VINEA_GATEWAY_URL   # 3
helm template vinea infra/chart --set gateway.enabled=true \
  --set gateway.externalUrl=https://llm.example.com | grep -c 'component: gateway'  # 0

# 7. The cost panel.
uv run streamlit run src/vinea/ui/app.py    # sidebar -> Cost
```

## The invariant

```bash
git diff --ignore-blank-lines phase-13 phase-14 -- \
  src/vinea/features.py src/vinea/contracts.py src/vinea/deps.py \
  src/vinea/graph.py src/vinea/reconcile.py src/vinea/pipeline.py     # must be empty
```

The gateway is configuration and the cost columns are persistence. Neither has
any business in the physics. If a model call has to change shape to be routed,
that is a signal the seam is in the wrong place — and the phase doc gets the
tension rather than the core getting the edit.
