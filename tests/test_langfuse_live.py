"""Langfuse, against a running instance. Skips with a reason when there isn't one.

Everything else about tracing is tested with an in-memory span exporter, which
proves the spans are *built* correctly and nothing about whether they can be
exported, accepted, processed and read back. Those are different claims, and the
second one had never been checked: the OTLP endpoint path, the basic-auth header
shape, and the fact that Langfuse v3 splits ingestion from processing — the web
service accepts spans and queues them in Redis, and a separate worker drains that
queue into ClickHouse. Without the worker, traces are accepted and then never
appear.

Start it with:

    docker compose --profile langfuse up -d
    export LANGFUSE_HOST=http://localhost:3000
    export LANGFUSE_PUBLIC_KEY=pk-lf-local-vinea
    export LANGFUSE_SECRET_KEY=sk-lf-local-vinea

The compose stack provisions the org, project and that keypair headlessly on first
boot, so those are the real values rather than placeholders.

These are slow by nature — a trace has to travel through Redis and ClickHouse
before it is queryable, which takes a few seconds. That is why they are gated on
the environment rather than run on every commit, and why CI does not start
Langfuse: ADR-004's stack is four stateful services, and paying that in every
pipeline to re-prove an export path buys very little.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

pytestmark = pytest.mark.langfuse

# How long to wait for a trace to become queryable. Generous: the span goes
# web -> Redis -> worker -> ClickHouse, and a cold worker is slower than a warm one.
TRACE_TIMEOUT_SECONDS = 90
POLL_SECONDS = 5


def _env() -> tuple[str, str, str]:
    host = os.environ.get("LANGFUSE_HOST")
    public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = os.environ.get("LANGFUSE_SECRET_KEY")
    if not (host and public and secret):
        pytest.skip(
            "LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY not set. "
            "`docker compose --profile langfuse up -d` and export them to run these."
        )
    return host.rstrip("/"), public, secret


def _api(path: str) -> dict:
    host, public, secret = _env()
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    request = urllib.request.Request(host + path, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


@pytest.fixture(scope="module")
def langfuse() -> str:
    """A reachable, authenticated Langfuse, or a skip.

    Checks health *and* credentials. A reachable instance that rejects the keys
    would otherwise fail every test below with an opaque 401 rather than one clear
    skip.
    """
    host, _, _ = _env()
    try:
        health = _api("/api/public/health")
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"Langfuse not reachable at {host}: {type(exc).__name__}")
    except urllib.error.HTTPError as exc:  # pragma: no cover - credential problem
        pytest.skip(f"Langfuse rejected the configured keys: HTTP {exc.code}")
    assert health.get("status") == "OK", health
    return host


def test_the_provisioned_project_exists(langfuse):
    """The headless provisioning worked, so no interactive signup is needed.

    If this fails the compose stack booted but `LANGFUSE_INIT_*` did not take
    effect, which is a silent failure otherwise: tracing would export to a host
    that authenticates nobody.
    """
    projects = _api("/api/public/projects")["data"]
    assert projects, "no project provisioned"
    assert any(p["id"] == "vinea" for p in projects), projects


def _grounded_advisory_trace() -> str:
    """Run the real graph under real export and return its trace id."""
    import logfire
    import pydantic_ai.models
    from pydantic_ai.models.test import TestModel

    from vinea import agents, config
    from vinea.deps import WINE_GRAPES
    from vinea.features import build_features
    from vinea.obs import tracing
    from vinea.obs.instrumented import run_advisory_instrumented
    from vinea.sources.csv_source import CsvSource

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False  # TestModel only; no live LLM

    handle = tracing.configure_tracing()
    assert handle.enabled, "the Langfuse span processor was not built from the environment"

    data_dir = Path(config.DEFAULT_DATA_DIR)
    run_date = date(2026, 7, 28)
    load_result = CsvSource(
        sorted(data_dir.glob("*last-30d*.csv"))[-1],
        sorted(data_dir.glob("*next-7d*.csv"))[-1],
        staleness_threshold_hours=48,
    ).load(run_date=run_date)
    features = build_features(
        list(load_result.history), list(load_result.forecast),
        load_result.quality, run_date, WINE_GRAPES,
    )

    target = features.target_date
    irrigation = {
        "target_date": target.isoformat(), "should_irrigate_tomorrow": True,
        "recommended_depth_mm": round(features.irrigation.current_depletion_mm, 1),
        "current_depletion_mm": features.irrigation.current_depletion_mm,
        "confidence": 0.4, "rationale": "live trace test",
    }
    spray = {
        "target_date": target.isoformat(), "can_spray_tomorrow": False,
        "recommended_windows": [], "limiting_factors": ["live trace test"],
        "confidence": 0.4, "rationale": "live trace test",
    }
    coordinator = {
        "summary": "live trace test", "conflicts_resolved": ["forced"],
        "overall_confidence": 0.4,
    }

    with (
        agents.irrigation_agent.override(model=TestModel(custom_output_args=irrigation)),
        agents.spray_agent.override(model=TestModel(custom_output_args=spray)),
        agents.coordinator_agent.override(model=TestModel(custom_output_args=coordinator)),
    ):
        result = run_advisory_instrumented(
            load_result, WINE_GRAPES, model="test:model", tenant="acme", run_date=run_date
        )

    assert result.trace_id, "no trace id was captured"
    logfire.force_flush()
    return result.trace_id


def _wait_for_trace(trace_id: str) -> dict:
    deadline = time.monotonic() + TRACE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        try:
            return _api(f"/api/public/traces/{trace_id}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    pytest.fail(
        f"trace {trace_id} never became queryable within {TRACE_TIMEOUT_SECONDS}s. "
        "Is langfuse-worker running? The web service accepts spans and queues them; "
        "the worker is what writes them to ClickHouse."
    )


def test_a_real_advisory_run_produces_a_readable_span_tree(langfuse):
    """The claim the trace exists to support, checked against the exported tree.

    `FeatureBuilderNode` must have **no GENERATION child**. That is the
    LLM/deterministic boundary made visible: the node that computes every number a
    grower sees never calls a model, and the trace shows it rather than a docstring
    asserting it.
    """
    trace_id = _grounded_advisory_trace()
    trace = _wait_for_trace(trace_id)

    observations = trace.get("observations", [])
    assert observations, f"trace {trace_id} has no observations"

    by_id = {o["id"]: o for o in observations}
    names = {o["name"] for o in observations}
    assert "advisory.run" in names, names
    assert any(n.startswith("run node FeatureBuilderNode") for n in names), names

    feature_nodes = [o for o in observations if o["name"].startswith("run node FeatureBuilderNode")]
    assert feature_nodes, "the deterministic node is missing from the trace"

    def descendants(root_id: str) -> list[dict]:
        out, frontier = [], [root_id]
        while frontier:
            current = frontier.pop()
            for obs in observations:
                if obs.get("parentObservationId") == current:
                    out.append(obs)
                    frontier.append(obs["id"])
        return out

    for node in feature_nodes:
        children = descendants(node["id"])
        generations = [c for c in children if c.get("type") == "GENERATION"]
        assert not generations, (
            "FeatureBuilderNode has a model call beneath it, so the "
            f"LLM/deterministic boundary has been crossed: {[g['name'] for g in generations]}"
        )

    # And the three agent legs DID call a model, or the tree proves nothing.
    assert sum(1 for o in observations if o.get("type") == "GENERATION") >= 3, [
        (o.get("type"), o["name"]) for o in observations
    ]
    assert by_id  # the id map is what descendants() relies on


def test_the_prompt_registry_round_trips_through_langfuse(langfuse):
    """Seed a version, fetch it by `name@label`, and confirm the drift check agrees.

    The registry's ladder is unit-tested with a stubbed fetcher. This is the part
    that stub cannot cover: the v2 prompts API, the label semantics, and that a
    freshly pushed version is what `production` resolves to.
    """
    from vinea.prompts import defaults, drift, langfuse_source

    for name in (defaults.IRRIGATION, defaults.SPRAY, defaults.COORDINATOR):
        bundled = defaults.BUNDLED_DEFAULTS[name]
        created = langfuse_source.push_prompt(name, bundled, labels=["production"])
        assert created.get("version"), created

        template, version = langfuse_source.fetch_prompt(name, "production", deadline=10.0)
        assert template == bundled, f"{name} came back different from what was pushed"
        assert version

    # With production pointing at the bundled text, nothing has drifted. The check
    # is only meaningful because the push above made it *able* to drift.
    results = drift.check_drift()
    drifted = [r for r in results if r.drifted]
    assert not drifted, [(r.name, r.detail) for r in drifted]
    assert len(results) == 3, results
