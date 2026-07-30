"""Run the graph inside a root span, and capture what the trace can't.

Two things the SDKs' automatic spans can't know on their own:

  1. The provenance tags -- model_id, degraded, tenant, run_date. These are ours,
     so we open a root span and set them, and every node/model span nests
     underneath.
  2. The PRE-correction model output. The output_validator corrects a bad number
     before it ships, and the automatic span only ever sees the corrected run. To
     keep the async eval honest -- the circularity trap below -- we wrap the run in
     `capture_run_messages` and pull out the model's first attempt -- the one the
     guardrail rejected -- so it can be stored and scored later against ground truth.

The circularity, stated plainly: the oracle both *corrects* (inline, as the
validator) and *scores* (async, over historical runs). If the async eval scores
the corrected output, it reports success on a model that got the number wrong
every time -- a number true about the guardrail, false about the model. The fix is
to score the pre-correction attempt, which means capturing it here, because
nothing else in the system ever sees it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from opentelemetry import trace
from pydantic_ai import capture_run_messages
from pydantic_ai.messages import RetryPromptPart, ToolCallPart

from vinea.contracts import DailyFarmAdvisory
from vinea.deps import Deps
from vinea.gateway.ledger import RunCost, ledger_scope
from vinea.graph import run_advisory_sync
from vinea.ingest import WeatherLoadResult
from vinea.obs.tracing import current_trace_id
from vinea.rag.citations import RetrievedPassage, citation_scope


@dataclass
class InstrumentedResult:
    """An advisory plus the observability metadata gathered while producing it."""

    advisory: DailyFarmAdvisory
    trace_id: str | None
    pre_correction_output: dict | None
    retried: bool
    # All-NULL when no gateway is configured and no model was metered --
    # the row then says "unknown", which is what it is.
    cost: RunCost = RunCost(input_tokens=None, output_tokens=None, cost_usd=None, cache_hit=None)
    # The passages retrieval SHOWED the model, per leg. Empty when no
    # corpus is ingested, which is the fail-open floor: no citations, never a weak
    # one. Deliberately not "the sources the model used" -- that would be a
    # self-report, and self-report is not evidence.
    passages: list[RetrievedPassage] = field(default_factory=list)


def _extract_pre_correction(messages: list) -> tuple[dict | None, bool]:
    """From a captured message list, the first tool-call the validator rejected.

    Walk the messages in order. If a `RetryPromptPart` appears, a validator forced
    a correction, and the tool call *before* the first retry prompt is the
    pre-correction attempt -- the model's own wrong answer. If no retry ever
    happened, there's nothing to log (the shipped output IS what the model said),
    so return None.

    Returns (pre_correction_args, retried).
    """
    first_tool_call: dict | None = None
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart) and first_tool_call is None:
                first_tool_call = part.args_as_dict()
            if isinstance(part, RetryPromptPart):
                # A retry was forced; the first tool call we saw is the
                # pre-correction attempt.
                return first_tool_call, True
    return None, False


def run_advisory_instrumented(
    load_result: WeatherLoadResult,
    deps: Deps,
    *,
    model: str,
    tenant: str,
    run_date: date,
    degraded: bool = False,
    prompt_version: str | None = None,
) -> InstrumentedResult:
    """Run the full graph under a tagged root span, capturing trace id + pre-correction.

    The root span makes the provenance tags apply to the whole tree and yields a
    trace id to store on the advisory. `capture_run_messages`
    wraps the run so a validator-forced correction leaves a record of what the
    model said first.

    `model` is provenance only -- Vinea's agents bind `model=config.MODEL` at run
    time, so the tag records what produced the advisory without changing how it
    runs. Assumes `configure_tracing` has already run; if it hasn't, the spans are
    no-ops and `trace_id` comes back None, which is the correct behaviour for a
    deployment with telemetry off -- the advisory is produced either way.
    """
    tracer = trace.get_tracer("vinea.advisory")

    with tracer.start_as_current_span("advisory.run") as span:
        # OTel GenAI-style tags + our own. These sit on the root and describe the
        # whole run, so a bad advisory is traceable to exactly which prompt version
        # and model produced it.
        span.set_attribute("vinea.tenant", tenant)
        span.set_attribute("vinea.run_date", run_date.isoformat())
        span.set_attribute("vinea.model_id", model)
        span.set_attribute("vinea.degraded", degraded)
        if prompt_version is not None:
            span.set_attribute("vinea.prompt_version", prompt_version)

        trace_id = current_trace_id()

        # The ledger wraps the run rather than living inside the graph, for the
        # same reason the root span does: the graph computes the advice and has no
        # business knowing that anyone is counting. It survives the `asyncio.run`
        # inside `run_advisory_sync` because a ContextVar holding a *mutable*
        # object is copied by reference into the new task's context -- appends
        # from in there land on this object.
        with ledger_scope() as ledger, citation_scope() as citations, capture_run_messages() as messages:
            advisory = run_advisory_sync(
                list(load_result.history),
                list(load_result.forecast),
                load_result.quality,
                run_date,
                deps,
            )

        cost = RunCost.from_ledger(ledger)
        passages = list(citations.passages)
        span.set_attribute("vinea.retrieved_passages", len(passages))
        pre_correction, retried = _extract_pre_correction(list(messages))
        span.set_attribute("vinea.retried", retried)
        # On the span as well as the row. The row is what an operator queries a
        # month later; the span is what they look at while the night is still
        # running, and a trace that shows latency but not spend answers half the
        # question people actually have about an LLM system.
        if cost.input_tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", cost.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", cost.output_tokens or 0)
        if cost.cost_usd is not None:
            span.set_attribute("vinea.cost_usd", cost.cost_usd)
        if cost.cache_hit is not None:
            span.set_attribute("vinea.cache_hit", cost.cache_hit)

    return InstrumentedResult(
        advisory=advisory,
        trace_id=trace_id,
        pre_correction_output=pre_correction,
        retried=retried,
        cost=cost,
        passages=passages,
    )
