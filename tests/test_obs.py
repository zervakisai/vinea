"""phase 9 (S4) -- observability: the span tree, the boundary visible, trace ids, pre-correction.

Everything here is offline. The trace goes to an in-memory OTel exporter, and the
models are FunctionModels, so no network and no real Langfuse -- the span *tree*
is what's under test, and it has the same shape whether the exporter is in-memory
or Langfuse.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from vinea import agents, config
from vinea.deps import WINE_GRAPES
from vinea.features import build_features
from vinea.ingest import WeatherLoadResult, load_weather
from vinea.obs import tracing
from vinea.obs.instrumented import run_advisory_instrumented

RUN_DATE = date(2026, 7, 28)
TARGET = RUN_DATE + timedelta(days=1)


@pytest.fixture
def span_exporter():
    """Configure tracing to an in-memory exporter; yield it; reset after."""
    exporter = InMemorySpanExporter()
    tracing.configure_tracing(processor=SimpleSpanProcessor(exporter), include_content=False)
    yield exporter
    exporter.clear()


def _load():
    data_dir = config.DEFAULT_DATA_DIR
    history = sorted(Path(data_dir).glob("*last-30d*.csv"))[-1]
    forecast = sorted(Path(data_dir).glob("*next-7d*.csv"))[-1]
    hist, fc, dq = load_weather(history, forecast, RUN_DATE)
    return WeatherLoadResult(history=hist, forecast=fc, quality=dq)


def _features():
    lr = _load()
    return lr, build_features(list(lr.history), list(lr.forecast), lr.quality, RUN_DATE, WINE_GRAPES)


def _tool(args: dict, info: AgentInfo) -> ModelResponse:
    part = ToolCallPart(tool_name=info.output_tools[0].name, args=args, tool_call_id="c")
    return ModelResponse(parts=[part])


def _grounded(features):
    def irr(m: list[ModelMessage], i: AgentInfo) -> ModelResponse:
        depletion = features.irrigation.current_depletion_mm
        return _tool(
            {
                "target_date": TARGET.isoformat(),
                "should_irrigate_tomorrow": True,
                "recommended_depth_mm": round(depletion, 1),
                "current_depletion_mm": depletion,
                "confidence": 0.3,
                "rationale": "t",
                "evidence": [],
            },
            i,
        )

    def spr(m: list[ModelMessage], i: AgentInfo) -> ModelResponse:
        return _tool(
            {
                "target_date": TARGET.isoformat(),
                "can_spray_tomorrow": False,
                "recommended_windows": [],
                "limiting_factors": ["t"],
                "confidence": 0.3,
                "rationale": "t",
                "evidence": [],
            },
            i,
        )

    def coord(m: list[ModelMessage], i: AgentInfo) -> ModelResponse:
        return _tool({"summary": "p", "conflicts_resolved": [], "overall_confidence": 0.1}, i)

    return irr, spr, coord


def _run(features, load_result, *, tenant="acme"):
    irr, spr, coord = _grounded(features)
    with (
        agents.irrigation_agent.override(model=FunctionModel(irr)),
        agents.spray_agent.override(model=FunctionModel(spr)),
        agents.coordinator_agent.override(model=FunctionModel(coord)),
    ):
        return run_advisory_instrumented(
            load_result, WINE_GRAPES, model="function:test", tenant=tenant, run_date=RUN_DATE
        )


def _is_llm_span(span) -> bool:
    return any(k.startswith("gen_ai") for k in span.attributes)


# --- S4.2: the span tree, with the boundary visible -------------------------


def test_the_graph_emits_a_span_per_node(span_exporter):
    lr, features = _features()
    _run(features, lr)

    names = [s.name for s in span_exporter.get_finished_spans()]
    for node in ("FeatureBuilderNode", "IrrigationNode", "SprayNode", "CoordinatorNode"):
        assert any(node in n for n in names), f"no span for {node} in {names}"


def test_feature_builder_is_a_visible_zero_token_span(span_exporter):
    """The whole point of S4.2: the boundary is a shape you can see. The
    FeatureBuilder node has a span, and it is NOT an LLM span."""
    lr, features = _features()
    _run(features, lr)

    spans = span_exporter.get_finished_spans()
    fb = next(s for s in spans if "FeatureBuilderNode" in s.name)
    assert not _is_llm_span(fb), "FeatureBuilder must have no gen_ai attributes -- it calls no model"

    # ...while the agent nodes DO produce LLM spans. The contrast is the boundary.
    llm_spans = [s for s in spans if _is_llm_span(s)]
    assert len(llm_spans) >= 3, "the three agents should each produce an LLM span"


def test_include_content_false_keeps_grower_numbers_out_of_spans(span_exporter):
    """ADR-004: include_content=False, so span payloads never carry the prompt text
    (which holds the grower's depletion figures and location)."""
    lr, features = _features()
    _run(features, lr)

    depletion = str(features.irrigation.current_depletion_mm)
    for span in span_exporter.get_finished_spans():
        for key, value in span.attributes.items():
            if "content" in key.lower() or key in ("gen_ai.prompt", "gen_ai.completion"):
                assert depletion not in str(value)


# --- S4.3: trace id ---------------------------------------------------------


def test_run_returns_a_trace_id_that_ties_to_the_exported_spans(span_exporter):
    lr, features = _features()
    result = _run(features, lr)

    assert result.trace_id is not None
    assert len(result.trace_id) == 32  # 32-char hex, Langfuse/OTLP format

    exported_trace_ids = {
        format(s.context.trace_id, "032x") for s in span_exporter.get_finished_spans()
    }
    assert result.trace_id in exported_trace_ids


def test_root_span_carries_the_provenance_tags(span_exporter):
    lr, features = _features()
    _run(features, lr, tenant="acme")

    root = next(s for s in span_exporter.get_finished_spans() if s.name == "advisory.run")
    assert root.attributes["vinea.tenant"] == "acme"
    assert root.attributes["vinea.model_id"] == "function:test"
    assert root.attributes["vinea.degraded"] is False


# --- S4.4: pre-correction capture (the circularity trap) --------------------


def test_no_retry_means_no_pre_correction_output(span_exporter):
    lr, features = _features()
    result = _run(features, lr)
    # The grounded models pass first try, so there's nothing to log -- the shipped
    # output IS what the model said.
    assert result.retried is False
    assert result.pre_correction_output is None


def test_a_forced_correction_is_captured_before_the_validator_fixed_it(span_exporter):
    """B2's circularity trap: the async eval must score what the model said, not
    what the guardrail shipped. So a validator-forced retry leaves the
    pre-correction attempt behind."""
    lr, features = _features()
    correct = features.irrigation.current_depletion_mm
    calls = {"n": 0}

    def flaky(m: list[ModelMessage], i: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        # First attempt is WRONG (off by 25mm -> validator forces a retry), second
        # is correct.
        depletion = correct if calls["n"] > 1 else correct + 25.0
        return _tool(
            {
                "target_date": TARGET.isoformat(),
                "should_irrigate_tomorrow": True,
                "recommended_depth_mm": round(depletion, 1),
                "current_depletion_mm": depletion,
                "confidence": 0.3,
                "rationale": "t",
                "evidence": [],
            },
            i,
        )

    _, spr, coord = _grounded(features)
    with (
        agents.irrigation_agent.override(model=FunctionModel(flaky)),
        agents.spray_agent.override(model=FunctionModel(spr)),
        agents.coordinator_agent.override(model=FunctionModel(coord)),
    ):
        result = run_advisory_instrumented(
            lr, WINE_GRAPES, model="function:test", tenant="acme", run_date=RUN_DATE
        )

    assert result.retried is True
    assert result.pre_correction_output is not None
    # The captured attempt is the WRONG one (the shipped advisory has the right
    # number). Scoring this, not the shipped output, is what keeps the eval honest.
    assert result.pre_correction_output["current_depletion_mm"] == pytest.approx(correct + 25.0)
    assert result.advisory.irrigation.current_depletion_mm == pytest.approx(correct)
