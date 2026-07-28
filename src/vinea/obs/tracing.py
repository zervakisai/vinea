"""Configure OpenTelemetry once, and expose the current trace id.

Two instrumentation paths have to feed ONE trace or the boundary picture falls
apart:

  1. pydantic-graph emits a span per node (`auto_instrument`, on by default), via
     logfire.
  2. pydantic-ai emits a span per model call with OTel GenAI attributes, when
     `Agent.instrument_all()` is active.

`configure_tracing` wires both to the same provider by configuring logfire (which
owns the global OTel `TracerProvider`) and then calling `instrument_all` with
settings that inherit it. After that, the graph's node spans and the agents' model
spans land in one tree, and FeatureBuilder is visibly the node with no gen_ai
child.

`include_content=False` is not an afterthought (ADR-004): span payloads would
otherwise carry the prompt and response text, and the prompts carry the grower's
depletion figures, block location, and yield-adjacent numbers. The trace records
*that* a model was called, with what token cost and which version -- never the
grower's numbers themselves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanProcessor
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings

SERVICE_NAME = "vinea-advisory"


@dataclass
class TracingHandle:
    """What `configure_tracing` hands back, for tests to inspect and reset."""

    processor: SpanProcessor | None
    enabled: bool


def _langfuse_processor() -> SpanProcessor | None:
    """Build an OTLP span processor pointing at Langfuse, or None if unconfigured.

    Langfuse speaks OTLP/HTTP and authenticates with basic auth over the
    public/secret key pair (ADR-004). Absent keys -> None, and tracing simply
    doesn't export anywhere, which is the correct degrade: no telemetry is not an
    error, it's a deployment that hasn't turned it on.
    """
    host = os.environ.get("LANGFUSE_HOST")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not (host and public_key and secret_key):
        return None

    import base64

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    exporter = OTLPSpanExporter(
        endpoint=f"{host.rstrip('/')}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {auth}"},
    )
    return BatchSpanProcessor(exporter)


def configure_tracing(
    *,
    processor: SpanProcessor | None = None,
    include_content: bool = False,
    console: bool = False,
) -> TracingHandle:
    """Configure logfire + pydantic-ai instrumentation against one provider.

    `processor` overrides the exporter -- tests pass an in-memory one; omit it and
    the Langfuse processor is used if its env is set. `send_to_logfire` is always
    False: we self-host (ADR-004), so nothing goes to Pydantic's SaaS.

    Idempotent enough for tests: logfire tolerates reconfiguration, and
    `instrument_all` just re-points the agents.
    """
    import logfire

    span_processor = processor if processor is not None else _langfuse_processor()

    logfire.configure(
        send_to_logfire=False,
        service_name=SERVICE_NAME,
        console=console,
        additional_span_processors=[span_processor] if span_processor is not None else [],
    )
    # The agents inherit logfire's global provider. include_content=False keeps
    # grower numbers out of span payloads (ADR-004).
    Agent.instrument_all(InstrumentationSettings(include_content=include_content))

    return TracingHandle(processor=span_processor, enabled=span_processor is not None)


def current_trace_id() -> str | None:
    """The active trace id as a 32-char hex string, or None outside a trace.

    This is what gets written to `advisories.trace_id` so the UI (S6) can deep-link
    to the trace. Formatted the way OTLP/Langfuse expect: lowercase hex,
    zero-padded to 32 chars.
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")
