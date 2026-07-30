"""Observability: trace the graph's own structure, not a flat call log.

The thing that differentiates this system from a demo is that you
can look at a trace and *see the boundary* -- a span per node, FeatureBuilder
included and visibly NOT an LLM span. This package makes that real:

  tracing.py       configure OpenTelemetry once; point it at Langfuse or, in
                   tests, an in-memory exporter. Sets include_content=False.
  instrumented.py  run the graph inside a root span carrying the provenance tags,
                   capture the trace_id and the PRE-correction model output, and
                   hand both back for persistence.

The pleasant surprise is how little code this takes: pydantic-graph already emits
a span per node (auto_instrument), and pydantic-ai already emits a span per model
call with OTel GenAI attributes. So the boundary is visible for free -- a
FeatureBuilderNode span with no gen_ai.* attributes sitting next to
IrrigationNode's span that has them. This package configures the exporter and adds
the two things the SDKs can't know: the provenance tags, and the pre-correction
capture that keeps the eval honest -- the circularity trap.
"""
