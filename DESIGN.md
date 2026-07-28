# DESIGN.md — scaling, evaluation & prompt management

This is the design essay behind phases 6–12. It was written *first*, at phase 5,
as research and proposal — before any of it was built. Reading it that way is the
point: it is the argument, and phases 6–12 are the argument carried out.

Three topics, referenced as **B1**, **B2** and **B3** throughout the code and the
ADRs (the `# TODO(B2)` markers in phase 1–4 code point here):

| | Topic | Built in | ADR |
|---|---|---|---|
| **B1** | Scaling — batch fan-out, model routing, retries, autoscale, tenancy | phases 6, 8 | ADR-001, ADR-003 |
| **B2** | Monitoring, debugging & improving the advice | phases 9, 12 | ADR-004 |
| **B3** | Managing prompts without redeploying | phase 12 | — |

Live data behind the `WeatherRow` seam (phase 7, ADR-002), the thin API (phase 10)
and the Streamlit dashboard (phase 11, ADR-005) fall out of B1's shape rather than
being topics of their own.

The **physics and the topology** from phases 1–4 survive all of it untouched —
`features.py`, `contracts.py`, `deps.py`, `graph.py`, `reconcile.py` and
`pipeline.py` are unchanged from `phase-04` to `phase-12`. `agents.py` does change,
in phase 12, when the instruction f-strings become registry lookups; that is B3
arriving, not the reasoning moving. The sections below are the *why*; the ADRs
record *what was decided*; the code is *how*.

Vinea vineyard advisor — scaling, observability/evaluation, and prompt management for the 3-agent
graph from the core. Each topic gives concrete tools, trade-offs, and a recommendation. The
through-line: because the agronomy is **deterministic** and the crop is **injected config**, most load
never hits a model, new crops/regions are config changes, and we have **ground truth** to hold the LLM
accountable.

## B1 — Scaling

Part A is a single async graph: `FeatureBuilderNode` (deterministic, no model) → `IrrigationNode` → `SprayNode` → `CoordinatorNode`, with crop params injected via `deps_type=Deps`, the model bound **at run time** (`model=config.MODEL` passed to `agent.run`, not at construction), and the day's prompt framed at runtime through dynamic `@agent.instructions`. Everything below scales *that* shape from one vineyard to N growers × many crops × many regions. The load-bearing fact: the agronomy is deterministic Python (FAO‑56 water balance + Delta‑T/wind gating), so **the LLM is a bounded judgement‑and‑explanation layer, not the compute** — most load never reaches a model, and a new crop/region is a config change, not a retrain.

### 1. Concurrency & throughput
"Tomorrow's advice" is computed in an overnight batch window (e.g. 02:00 local per region), one queued task per grower, drained by an async worker pool. Part A already calls `await agent.run(...)` (never `run_sync`), so a worker can hold hundreds of in-flight runs that are almost all blocked on the provider socket. This workload is **throughput-bound, not latency-bound** — nobody is waiting on the 02:00 run — so it is the ideal fit for provider **Batch APIs** (OpenAI Batch / Anthropic Message Batches: ~50% cheaper, 24h SLA). We submit the night's prompts as a batch and reap results before the grower wakes. A separate, latency-optimised **synchronous "ask now"** path (streaming `agent.run`, small model, tight deadline) handles the occasional interactive query; it does *not* go through Batch.
**Recommendation:** queue-drained async fan-out for the nightly run, submitted via Anthropic Message Batches for the LLM legs; reserve real-time `agent.run` only for an interactive "ask now" endpoint.

### 2. Latency & cost
The structural win comes first: the deterministic boundary means the bulk of compute is cheap CPU (pandas over a 720‑row history / 168‑row forecast), and many decisions are *settled before any model call* — with the real data (30‑day history 0 mm rain, cumulative ETc 161.76 mm; forecast ~6.06 mm/day tomorrow, 0 mm rain, depletion clamped at TAW=150 mm, well past the RAW/MAD trigger of 67.5 mm) irrigation is near-certain and midday spray is already excluded (Delta‑T tops 13.5 °C, >10 °C ceiling; wind to ~6.7 m/s). On top of that:
- **Caching, exact/region-scoped first.** The spray candidate windows are a pure function of *region weather + crop thresholds*, not per-grower soil, so one region-shared forecast collapses the whole spray FeatureBuilder pass for every grower of that crop in the region (Part A's deterministic windows: pre-dawn 02:00–05:00 + 16:00–23:00). Cache on `(region, crop, date)`; reach for **semantic caching** only if exact keys prove too sparse — it adds an embedding hop and false-hit risk we don't need here.
- **Small-vs-large model routing keyed off the deterministic features.** Because Part A binds the model at run time, the router simply passes a different `model=` to `agent.run` per call — no construction change. Clear-cut calls (trigger clearly crossed, 0 mm rain, no candidate windows) route to a small/cheap model — or skip the LLM decision entirely and just generate the explanation; only borderline deficit-irrigation calls and genuine irrigation-vs-spray reconciliation get the large model (the Part A default `anthropic:claude-sonnet-4-5`).
**Recommendation:** region-shared forecast + exact `(region,crop,date)` cache, deterministic-feature router (a different run-time `model=`) sending the clear majority to a small model and reserving Sonnet for borderline deficit decisions and the coordinator.

### 3. Reliability
- **429s:** exponential backoff **with jitter, respecting `Retry-After`** — never a fixed retry cadence, which synchronises clients into a thundering herd.
- **The double-retry footgun:** Part A sets `retries=2` on every agent (re-asks the model on `ModelRetry`/validation failure). Wrapping `agent.run` in an outer infra retry (e.g. tenacity ×3) *multiplies*: 2 inner × 3 outer ≈ 6–9 model calls per logical request — token-budget blowups and **retry amplification** that turns a brief rate-limit blip into a metastable storm. Own retries in **one** layer: keep pydantic-ai's `retries` for *validation* re-asks, and do *not* add an outer retry around transport (or vice-versa), never both.
- **Timeouts as deadlines:** a per-attempt timeout makes wall-clock = `timeout × attempts`. Propagate one request **deadline** that shrinks across retries, so the third attempt gets whatever budget remains, not a fresh full timeout.
- **Circuit breakers per provider:** closed → open (fail fast / shed load when a provider degrades) → half-open (probe recovery), so we stop piling requests onto a sick endpoint.
- **Cross-provider `FallbackModel`** — with a caveat. It swaps providers transparently, but providers enforce typed output differently (OpenAI strict JSON-schema/tool-calling vs Anthropic tool use), so a fallback may not honour the *same* `output_type` contract (`IrrigationAdvice`, etc.) — validation can **silently degrade or churn `ModelRetry`** instead of hard-failing.

```python
from pydantic_ai.models.fallback import FallbackModel
model = FallbackModel(anthropic_model, openai_model)  # MUST pin each model's output mode + run the
# fallback through the SAME output_type + eval set (B2) before trusting it in prod
```

**Recommendation:** single retry layer with jittered, `Retry-After`-aware backoff under a shrinking deadline; per-provider circuit breaker; `FallbackModel` only after the secondary is eval-gated (B2) against the identical schema with a pinned output mode.

### 4. Horizontal scale
Workers are **stateless**: the per-grower depletion carry-over and weather history live in a per-grower store (Part A recomputes depletion forward from history each run; that history, not the worker, is the durable state), so any worker can pick up any task and the pool scales freely. Crucially, **autoscale on queue depth / in-flight concurrency, not CPU**: the LLM tier is I/O-bound — workers sit blocked on the provider, so CPU stays low even at full saturation and CPU-based autoscaling under-provisions. Split the two tiers with their different signals and failure domains:
- **Deterministic tier** (FeatureBuilder, genuinely CPU-bound over the history/forecast frames) — scale on CPU.
- **Thin LLM tier** (the three agent legs, I/O-bound) — scale on queue depth/age and in-flight count.
**Recommendation:** stateless workers over a per-grower state store; two independently-scaled tiers (CPU-bound deterministic, queue-depth-scaled LLM); the graph docstring's `asyncio.gather` upgrade note (a TODO, not yet wired) would let the independent Irrigation/Spray legs run concurrently within a worker.

### 5. Multi-tenancy & per-region
- **Per-tenant token budgets.** Thread one `RunUsage` through `FarmState` and pass it (with a `UsageLimits` cap) to each `agent.run`, so the three legs aggregate into a single per-tenant total. This is exactly the `TODO(B2)` the graph docstring flags — *a seam, not wired*: today the legs run un-metered.

```python
from pydantic_ai.usage import RunUsage, UsageLimits
run_usage = RunUsage()                       # one per tenant-night, threaded through FarmState (graph TODO)
await agent.run(prompt, deps=deps, model=config.MODEL,
                usage=run_usage,             # the 3 legs aggregate into one tenant total
                usage_limits=UsageLimits(total_tokens_limit=tenant.nightly_budget))
```

- **Tenant-namespaced caches** so no cross-grower cache bleed.
- **EU data residency** for the Peloponnese growers: route their inference to EU endpoints/regions, keep their state store in-region.
- **New crop/region = config, not a model change.** Adding table grapes is one object — `Deps(crop="table grapes", kc=0.85)` — flowing through `deps_type` and the dynamic `@agent.instructions` framing; a new region adds a config row plus a region-namespaced prompt **label** (B3). No retrain, no redeploy. This only holds because the LLM/deterministic boundary was drawn correctly.
**Recommendation:** per-tenant `UsageLimits` budgets over a threaded `RunUsage`, namespaced caches, EU-pinned endpoints/storage for Greek growers, and crop/region onboarding as a `Deps` row + prompt label.

### Recommended architecture
A nightly, per-region scheduler fans out one task per grower onto a durable queue. A **stateless LLM worker pool** (autoscaled on queue depth) drains it; each grower runs the Part A graph where the **CPU-bound FeatureBuilder** runs against a **region-shared forecast** and a per-grower depletion store, then a **deterministic-feature router** sends the clear majority to a small model (often skipping the decision call entirely) and only borderline deficit/reconciliation cases to Sonnet via **Anthropic Message Batches**. One retry layer with jittered `Retry-After` backoff under a shrinking deadline, per-provider circuit breakers, and an eval-gated `FallbackModel` guard reliability. Per-tenant `UsageLimits`, namespaced caches, and EU endpoints handle tenancy. At realistic scale — **N growers × ≤3 agent runs/night**, most short-circuited by the deterministic boundary and a region-shared forecast — the heavy LLM cost collapses to the handful of genuinely borderline vineyards per region per night.


---

## B2 — Monitor, debug & improve the AI advice

The thesis of this submission is that the LLM is a *bounded judgement-and-explanation layer* over deterministic agronomy. That same boundary is what makes B2 unusually strong: because the physics in `FeatureBuilder` is exact, we have **ground truth**, so for the irrigation/spray *decisions* we can score the model precisely rather than guess with an LLM judge. The job is to (1) see what the three agents did, (2) measure them against that ground truth, and (3) close the loop. Note up front: Part A leaves instrumentation as a `TODO(B2)` seam beside each `Agent(...)` (the code already points at `capabilities=[Instrumentation(...)] + Logfire`) — nothing below is wired yet.

### Tracing: a span tree, not flat logs

The advisory is a graph — `FeatureBuilderNode → Irrigation → Spray → Coordinator`. A flat log line per call loses the parent/child causality, so it can't answer the question that actually comes up in support: *"why did the Coordinator drop the 16:00–23:00 spray window?"* A trace makes the **graph run the root span** with `FeatureBuilder → Irrigation → Spray → Coordinator` as ordered child spans; the Coordinator span carries the deterministic `conflict_facts` and the final `Reconciliation` (the two sub-advices it reconciles are the preceding sibling spans — re-attached verbatim in code, so the LLM never echoes them), with per-node latency and per-node token/cost attribution. For a 3-agent graph that is the difference between debuggable and not.

Emit **OpenTelemetry GenAI semantic conventions** (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`/`output_tokens`, `gen_ai.operation.name`) so the backend is swappable — instrument once, export OTLP anywhere. Pydantic AI is first-party here: spans come from the `Instrumentation` capability — `capabilities=[Instrumentation(settings=InstrumentationSettings(include_content=False))]` (the 1.107 surface; the legacy `instrument=` kwarg is *deprecated* in favour of exactly this) — exported through Logfire via `logfire.configure(); logfire.instrument_pydantic_ai()`. That is what lands at the `TODO(B2)` next to each `Agent(...)`.

| Backend | Model | Strength | Cost / watch-out |
|---|---|---|---|
| **Pydantic Logfire** | SaaS + OTLP | First-party pydantic-ai spans, OTel-native, can self-route | EU region exists; SaaS by default |
| **Langfuse** | OSS, self-host | Tracing **+ evals + prompt mgmt** in one (ties B3); OTLP ingest | You run it; UI less polished |
| **LangSmith** | SaaS | Mature eval UX | LangChain-centric; US-hosted; vendor lock |
| **Arize Phoenix** | OSS, self-host | Strong drift/embedding analysis | Less prompt-mgmt; more setup |
| **Helicone** | Proxy/gateway | Drop-in, cheap caching/metrics | Proxy sits in the request path; shallower graph view |

**Recommendation: self-hosted Langfuse, fed via OTLP.** Grower agronomic data is proprietary, the growers are in the Peloponnese (EU data-residency), and Langfuse co-locates tracing + evals + the B3 prompt registry — one system, in our VPC. If we adopt Logfire for the developer experience, run it `send_to_logfire=False` with OTLP export to that self-hosted backend. Either way, **redact content**: `InstrumentationSettings(include_content=False)` so prompts/outputs (grower numbers, parcel detail) never leave as span payloads — we keep the structure and token counts, not the PII.

### The differentiator: deterministic ground-truth oracles

LLM-as-judge is the *wrong default* here because we can compute the right answer. We wrap Part A's own functions as `pydantic-evals` evaluators so the eval and the product share **one** implementation of the water balance.

**`WaterBalanceOracle`** re-runs the FAO-56 balance `depletion(t)=max(0, depletion(t-1)+ETc−eff_rain)` (ETc = ET0·Kc, Kc=0.70) and triggers on the **MAD/RAW** threshold (raw_mm = 0.45·150 = **67.5 mm**). On the real data this is a *strong, checkable label*: 30 days at 0 mm rain drove cumulative ETc to 161.76 mm, so depletion is clamped at TAW=150 mm — roughly **2.2× the 67.5 mm trigger** — with ~6.06 mm/day demand tomorrow and 0 mm forecast rain. The oracle says *irrigate is near-certain*, so here a "don't irrigate" is a hard fail; **nearer the trigger** — where deficit-irrigation judgement legitimately lets the model hold off — disagreement is graded and flagged for the agronomist rather than auto-failed (the threshold itself is the subjective slice the oracle defers, see the dual-role note below).

```python
class WaterBalanceOracle(Evaluator[FarmInputs, IrrigationAdvice]):
    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        f = build_irrigation_features(ctx.inputs.history, ctx.inputs.forecast,
                                      ctx.inputs.deps, ctx.inputs.run_date)   # Part A, verbatim
        oracle, got = f.should_irrigate_trigger, ctx.output.should_irrigate_tomorrow
        if got == oracle:
            return EvaluationReason(value=1.0, reason="matches RAW/MAD balance")
        # asymmetric: a missed real need is far costlier than a false alarm
        return EvaluationReason(value=-(5.0 if oracle and not got else 1.0),
                                reason=f"oracle={oracle} model={got}")
```

**`SprayRuleEvaluator`** scores `can_spray_tomorrow` and each window against the Delta-T bands (2–8 ideal, 8–10 marginal, >10 unsuitable, <2 inversion) and the wind band (0.83–4.2 m/s) by reusing `classify_delta_t`/`classify_wind`/`detect_spray_windows`. The forecast Delta-T span of **4.8–13.5 °C** plus wind up to **6.7 m/s** means the answer is checkable hour-by-hour: the deterministic windows are pre-dawn **02:00–05:00** and **16:00–23:00**, with midday correctly failing (wind-drift is the dominant gate; midday also exceeds the 10 °C ceiling). The provided **Spray Index (avg ~65, higher=better)** is an *opaque composite to cross-check, never to trust* — if the model leans on a high but pre-dawn-dark index hour, the rule engine catches it.

**Asymmetric error costs.** Under a deficit-irrigation regime a false-negative (told *don't* irrigate when the vine needed it) risks yield/quality on an already-stressed crop; a false-positive wastes water and mildly dilutes fruit. Encode it by weighting oracle disagreement (missed-irrigation **3–5×**, as above) and reporting **recall on `should_irrigate_tomorrow` as the gating metric**, separate from raw accuracy.

**Groundedness/consistency.** Rationale numbers must equal `FeatureBuilder`'s. Part A already enforces this for the headline number: `_validate_irrigation` raises `ModelRetry` if `current_depletion_mm` drifts >0.5 mm from the computed value. The B2 extension is to check every `evidence[].value` against the features dict, and to assert *same input → same decision* across reruns (a free determinism check the oracle gives us).

### Dual role of the oracle, and why it isn't circular

The same water-balance code plays two roles. **Inline** as an `@agent.output_validator` raising `ModelRetry` it *protects the live request* (bounded by Part A's `retries=2`). But this guard must stay narrow: the prompt deliberately allows the model to *override* the mechanical trigger as deficit-irrigation judgement on borderline calls — so the inline guard fires only on **physically indefensible** disagreement (depletion ≫ RAW *and* model says no), not on a marginal call.

```python
@irrigation_agent.output_validator   # TODO(B2): decision guard; Part A guards the NUMBER, not yet the call
def _indefensible(ctx, out):
    f = ctx.deps.features
    if not out.should_irrigate_tomorrow and f.current_depletion_mm > 1.5 * f.raw_mm \
       and f.effective_rain_tomorrow_mm < ctx.deps.crop.rain_skip_mm:
        raise ModelRetry("refusing to irrigate at >1.5× RAW with no rain is indefensible; reconsider")
    return out
```

**Async** as a `pydantic-evals` `Evaluator` over a golden set, it *measures* the model out of the request path. Running both is **not circular** because the inline validator always corrects the output before it ships, so post-correction it trivially "passes" — measuring that would hide the model's true weakness. So we **log the pre-correction output** (`capture_run_messages`) and run the oracle on *that*. One job protects the grower; the other measures the model and catches drift.

### Where the LLM judge is still valid

Reserve `LLMJudge` for the one thing without ground truth: the subjective grower-facing **`summary`** (clarity, actionability, sequencing read). LLM judges carry position, verbosity, self-enhancement and leniency biases, so we use a *different/stronger* judge model with an explicit rubric — and never let it grade the irrigation/spray decisions where the oracle exists.

### Offline gate vs online, and trajectory eval

- **Offline, CI-gated golden set** (`Dataset(cases=[Case(...)], evaluators=[WaterBalanceOracle(), SprayRuleEvaluator(), IsInstance(type_name='IrrigationAdvice')])`, `ds.evaluate_sync(task)`): runs on every prompt/model change and **gates promotion** (this is exactly the eval gate B3's registry checks before re-pointing a label).
- **Online scoring** of live traffic: the oracle is cheap and deterministic, so we can score *every* production advisory in the background and alert on a recall dip.
- **Trajectory, not just the final `DailyFarmAdvisory`.** Score each node's typed output and, critically, the **reconcile step**: the Coordinator emits only a `Reconciliation` (the sub-advices are re-attached in code, so only the reconciliation itself can drift) — did it genuinely sequence ("spray the 16:00 window, irrigate after sunset") using the `conflict_facts`, or merely restate the two legs? A right-looking final advisory can hide a broken Spray step or a lucky reconcile.

### Human-in-the-loop

Two queues, two questions. The **agronomist** labels *correctness* — above all the deficit-irrigation *threshold*, which is the legitimate subjective slice the oracle deliberately doesn't adjudicate. The **farmer** labels *usefulness/trust/clarity* of the summary. The closed loop: **prod trace → annotation queue → reviewed disagreement becomes a new `Case` → added to the regression-gated golden set**, weighted by reviewer role.

### Debugging & drift

- **Deterministic replay:** `agent.override(model=FunctionModel(fn))` with recorded messages, and `pydantic_ai.models.ALLOW_MODEL_REQUESTS=False` so CI can never make a live call — pinned inputs, pinned model behaviour.
- **Versioned runs + A/B/shadow:** every eval run is tagged with `prompt.version`/model id; shadow a candidate prompt against production, A/B by a stable hash on grower_id (ties B3).
- **Data-drift vs model-drift:** distinguish via **fixed-input golden replay**. If scores move on pinned inputs → *model/behaviour drift* (a silent provider model update or a prompt edit). If live scores fall while the golden set holds → *input/data drift* (new region/season, sensor recalibration). The deterministic oracle is what makes that distinction crisp rather than a vibe.

**Bottom line:** self-hosted Langfuse (EU, `include_content=False`) for the span tree; deterministic oracles as the primary evaluators with asymmetric, recall-gated scoring; the LLM judge confined to the summary; pre-correction logging so the inline guardrail never masks the measurement; and golden replay as the drift discriminator.


---

## B3 — Manage prompts without redeploying

The agronomy prompts are *product copy owned by agronomists*, not engineers. They change at the cadence of agronomy — a mid-season shift to deficit-irrigation framing, a region-specific spray caveat for a humid Peloponnese week — which is far faster than the cadence of code deploys. So the framing text must live **outside** the deploy artifact and be editable by a domain expert through a UI, while the *numbers* (Kc, Delta-T bands, the day's depletion) keep flowing through Part A's `deps`. Part A already drew the seam: each `@agent.instructions` renderer carries `# TODO(B3): swap this f-string for ctx.deps.registry.get('agronomy_advisor@production') + bundled fallback`, and `deps.py` notes "load a CropConfig from a registry by (crop, region)." This section is the design behind those TODOs.

### Externalise by `name@label`, not by version

Code references a **label**, never a frozen version number: the irrigation agent asks for `agronomy_advisor@production`, the spray agent for `spray_advisor@production`. **Labels are mutable pointers; versions are immutable.** This decoupling is the entire reason a prompt change needs no redeploy — shipping a new prompt is "save a new immutable version, then re-point the `production` label," both registry-side operations the binary never sees. A `staging`/`canary` label gives a soak path; a region suffix (`spray_advisor@production-gr`) namespaces per-region copy without forking code.

### In-process cache: TTL + stale-while-revalidate

Hitting the registry on every grower request would couple our hot path to its uptime and latency — unacceptable for an overnight batch of N growers × 3 agent runs. So each worker keeps a small in-process cache keyed on `name@label` with a **short TTL (~60 s)** and **stale-while-revalidate**: an expired entry is still served immediately while a background task refreshes it, so registry latency never lands on a request. This is not bespoke — Langfuse's SDK `get_prompt(name, label=..., cache_ttl_seconds=...)` implements exactly this and, critically, **serves the expired cache when a refresh fetch fails** (real, citable behaviour), which is the first rung of our fail-open ladder.

### Registry down on a live request → fail open (the load-bearing path)

This is the answer the rubric digs into. On every request, in strict order:

1. **Serve fresh in-process cache** — TTL-valid copy of `name@label`, zero network.
2. **On a miss, fetch with a tight timeout treated as a *deadline*** (not a per-attempt timeout that can stack) — a few hundred ms, single budget.
3. **On timeout / error / cold cache, serve the BUNDLED DEFAULT prompt** shipped *inside the deploy artifact* — version-pinned, and kept from rotting by a **CI check that diffs the bundled default against the current `production` version and fails the build on drift** beyond an allowed lag.
4. **Never raise.** Degraded mode is silent to the grower but loud to us: tag `prompt.source=fallback` and `prompt.version` on the trace (B2's spans) so it is observable and alertable.

The grower always gets advice from a known-good prompt regardless of registry state. Note the proprietary-data angle this protects: the registry holds *templates only* — the day's grower numbers (e.g. this vineyard's 30-day cumulative ETc of **161.76 mm** over **0 mm rain**, and the resulting pre-dawn 02:00–05:00 + 16:00–23:00 spray windows) are injected via `deps` at request time and **never sent to the registry**. A registry outage therefore degrades *wording*, never the decision physics.

### Options and the recommendation

- **SaaS / managed** — Langfuse Prompt Management, PromptLayer, Humanloop, LangSmith Prompt Hub, Agenta. UI for non-engineers, built-in versioning/labels, eval linkage, fastest to adopt; cost is a vendor dependency and (for some) your prompt IP living on their servers.
- **Self-hosted** — Langfuse OSS (or Agenta OSS): the same UI, labels, and `cache_ttl_seconds` fallback semantics, but templates and audit trail stay in-house, deployable to an EU region for the Greek growers.
- **DIY** — Postgres row table, S3 with object versioning, **Git-backed config** (commits = immutable versions, PR review = governance, `git revert` = rollback — but if baked into the image it needs a redeploy, defeating the goal unless paired with a runtime pull), or a **feature-flag platform** (LaunchDarkly) used purely as the label/pointer + gradual-rollout layer.

**Recommendation: self-host Langfuse OSS.** It gives the non-engineer UI, immutable-version/label model, and the serve-stale-on-failure cache we rely on for the fallback path — while keeping prompt IP and the audit trail on our infrastructure with EU residency. The proprietary-data nuance means SaaS *prompt management* is genuinely lower-risk than SaaS that sees inference traffic (it never sees grower numbers), but self-hosting is the right defence-in-depth here and it co-locates cleanly with the B2 tracing/eval backend (also Langfuse), so prompt versions and eval runs share one store.

### Versioning, A/B, and governance

Every save is an immutable version. **Rollback = re-point the label** — instant, no deploy. Gradual rollout / **A/B is a split by label chosen on a deterministic hash of `grower_id`** so a grower gets a stable variant across nights and the variant is tagged on the trace. Each version links to its **eval-score run from B2**, so promotion is data-gated rather than vibes-gated. Governance: agronomists edit **drafts in `staging`** via the UI; promotion `staging → production` requires **(a)** an eval pass against the deterministic oracles (the water-balance oracle for irrigation, the Delta-T/wind rule engine for spray) plus the golden set, and **(b)** a human **approver** re-pointing the label. Template inputs are a **typed Pydantic variable model**, so renaming `{{crop}} → {{cultivar}}` **fails fast at render** instead of silently producing a wrong prompt. A full **audit trail** (who / what / when / which eval) closes the loop.

### Pydantic AI seam (illustrative — not wired)

The fetch lands in the existing dynamic `@agent.instructions` function, which runs per request and reads `RunContext[Deps]`, so it always reflects whatever the label currently points to — no redeploy. (`instructions` is the right seam, not `system_prompt` — which Part A does not use: we don't want the framing carried into multi-turn history.)

```python
from opentelemetry import trace

@irrigation_agent.instructions
def _irr_instructions(ctx: RunContext[IrrDeps]) -> str:
    # TODO: wire to real registry client (Langfuse OSS) with cache + fallback
    p = registry.get("agronomy_advisor@production")           # SWR cache; serves stale on failure
    trace.get_current_span().set_attributes({                 # B2 trace attribution (OTel)
        "prompt.name": p.name, "prompt.version": p.version,
        "prompt.source": p.source,                            # "registry" | "cache" | "fallback"
    })
    return p.render(ctx=render_irrigation_context(ctx.deps))  # grower numbers injected here, never stored
```

The `render_irrigation_context(ctx.deps)` call is exactly today's code; only the surrounding prose moves to the registry, and `prompt.name:version` on the span makes **every output attributable to a prompt version**.

### What I deliberately did not propose

**Per-grower bespoke prompts.** They would explode the version surface (thousands of un-evaluable variants), break the deterministic A/B hashing, defeat caching (every grower a cache miss), and make the eval gate impossible to enforce. Per-grower *variation* belongs in `deps` (Kc, thresholds, the day's features) injected at request time — not in the prompt template. The template stays shared and governed; the grower-specific reality stays in typed dependencies.