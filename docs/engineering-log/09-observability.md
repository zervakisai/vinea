# Phase 9 — Observability

`git checkout phase-09`

## What you learn

Why a *tree* of spans tells you things a stream of log lines structurally cannot —
and that if you built the boundary properly, making it visible costs almost nothing.

## The central idea

[ADR-004](../adr/004-self-hosted-langfuse.md): self-hosted Langfuse over an
OTLP/HTTP exporter.

The pleasant surprise is how little code this takes. `pydantic-graph` already emits
a span per node (`auto_instrument`), and `pydantic-ai` already emits a span per
model call with OTel GenAI attributes. So the trace shows, for free:

```
advisory                          ← root span, provenance tags
├── FeatureBuilderNode            ← no gen_ai.* attributes.  Not an LLM call.
├── IrrigationNode
│   └── chat anthropic:claude…    ← gen_ai.* attributes
├── SprayNode
│   └── chat anthropic:claude…
└── CoordinatorNode
    └── chat anthropic:claude…
```

You can *see the boundary* in the trace. A `FeatureBuilderNode` span sitting next
to `IrrigationNode`'s, one with GenAI attributes and one without, is the phase-2
argument rendered as telemetry. Flat logs cannot show you that, because the thing
you want to see is the *shape*.

## Decisions

- **Self-hosted, `send_to_logfire=False`.** Logfire owns the global OTel provider
  because both SDKs emit into it; the OTLP exporter ships spans to a Langfuse you
  run. Agronomic advisories and grower identifiers do not need to leave your
  infrastructure to be debuggable.
- **`include_content=False`.** Prompt and completion bodies stay out of spans by
  default. Turn it on deliberately, for a debugging window, not as a standing
  policy.
- **The two things the SDKs cannot know**, added by `instrumented.py`:
  1. **Provenance tags** on the root span — `vinea.tenant`, `vinea.run_date`,
     `vinea.model_id`, `vinea.degraded`, `vinea.prompt_version`, `vinea.retried`.
     Without these a trace is an anonymous waveform.
  2. **The pre-correction capture** — the model's output *before* an
     `output_validator` corrected it. This is what keeps phase 12's eval honest.
- **Tests use an in-memory exporter.** Assertions run against real spans, offline,
  with no collector.

## The pre-correction capture

This is the subtle one, and it is the fix for a trap the design essay spotted on
paper (B2's circularity).

The same oracle is used in two places: **inline** as an `output_validator` that
raises `ModelRetry`, and **later** as the eval metric. If the eval scored the
*shipped* output, it would be scoring output the oracle had already corrected — and
it would report a perfect score forever, because the guardrail guarantees it.

So `instrumented.py` captures the model's first, uncorrected answer and hands it
back for persistence as `advisories.pre_correction_output`. The eval scores that.
The guardrail protects the grower; the eval measures the model.

## Read this

- `docs/adr/004-self-hosted-langfuse.md`
- `src/vinea/obs/tracing.py` — configure once, exporter swappable
- `src/vinea/obs/instrumented.py` — the root span, the tags, the capture
- `tests/test_obs.py` — asserting on span trees

## The trap

`include_content=False` means that when an advisory goes wrong in production, the
trace tells you *which* node, with what tags, after how many retries — and not what
the model was actually asked. That is the right default and it is genuinely
inconvenient at 2 a.m.

The mitigation is not to flip the flag permanently. It is to know that the
pre-correction output *is* persisted, so the model's answer survives even when the
prompt body does not, and the bundled prompt templates are in the repo. Between
those two you can usually reconstruct the call. "Usually" is the honest word.

## Try it

```bash
docker compose up -d postgres langfuse
uv run pytest tests/test_obs.py -v
```

Then read `test_obs.py`'s span-tree assertions and find the one that checks a
`FeatureBuilderNode` span has **no** `gen_ai.*` attributes. That test fails the day
someone moves the water balance inside an agent — which makes it the most valuable
test in the repository, because it guards an architectural decision rather than a
behaviour.
