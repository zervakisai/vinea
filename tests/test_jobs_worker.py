"""phase 8 (S3.2 / S3.5 / S3.6) -- the worker end to end, and the scheduler.

The worker touches a real database and the real routing/degraded logic, but the
LLM is always mocked (TestModel) or absent (no key), so nothing here makes a
network call -- ALLOW_MODEL_REQUESTS stays False. (The trace_id-through-the-queue
test lands in phase 9, once the instrumented runner exists.)
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel
from sqlmodel import Session, select

from vinea import agents, config
from vinea.db import repository
from vinea.db.models import AdvisoryTask, QueueDepthSample
from vinea.deps import WINE_GRAPES, Deps
from vinea.features import build_features
from vinea.ingest import load_weather
from vinea.jobs import queue, scheduler, worker

pytestmark = pytest.mark.db

RUN_DATE = date(2026, 7, 28)
TARGET = RUN_DATE + timedelta(days=1)
_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY")


def _session(engine) -> Session:
    """A committing session scoped to ops (phase 17).

    The worker and the queue are cross-tenant by design -- one queue, SKIP
    LOCKED, every tenant -- so the tests declare the same scope the worker
    declares. A bare `Session(engine)` would now see nothing.
    """
    from vinea.db.session import scope_to_ops

    session = Session(engine)
    scope_to_ops(session)
    return session


def _features():
    data_dir = config.DEFAULT_DATA_DIR
    history = sorted(Path(data_dir).glob("*last-30d*.csv"))[-1]
    forecast = sorted(Path(data_dir).glob("*next-7d*.csv"))[-1]
    hist, fc, dq = load_weather(history, forecast, RUN_DATE)
    return build_features(hist, fc, dq, RUN_DATE, WINE_GRAPES)


def _grounded_overrides(features):
    """TestModel outputs grounded against the features the worker will compute:
    the echoed depletion matches, and the spray leg declines rather than inventing
    a window -- so every output_validator passes with modest confidences well under
    any data-quality ceiling."""
    irr = {
        "target_date": TARGET.isoformat(),
        "should_irrigate_tomorrow": True,
        "recommended_depth_mm": round(features.irrigation.current_depletion_mm, 1),
        "current_depletion_mm": features.irrigation.current_depletion_mm,
        "confidence": 0.4,
        "rationale": "worker test",
        "evidence": [],
    }
    spr = {
        "target_date": TARGET.isoformat(),
        "can_spray_tomorrow": False,
        "recommended_windows": [],
        "limiting_factors": ["worker test: declined"],
        "confidence": 0.4,
        "rationale": "worker test",
        "evidence": [],
    }
    coord = {"summary": "test plan", "conflicts_resolved": [], "overall_confidence": 0.3}
    return (
        agents.irrigation_agent.override(model=TestModel(custom_output_args=irr)),
        agents.spray_agent.override(model=TestModel(custom_output_args=spr)),
        agents.coordinator_agent.override(model=TestModel(custom_output_args=coord)),
    )


# --- S3.5: the degraded path (no API key) -----------------------------------


def test_worker_with_no_api_key_writes_a_degraded_advisory(committing_db, monkeypatch):
    for var in _KEYS:
        monkeypatch.delenv(var, raising=False)

    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        s.commit()

    with _session(committing_db) as s:
        task = queue.claim_one(s, worker_id="w1")
        result = worker.process_one(s, task)

    assert result.status == "done"
    assert result.degraded is True
    assert result.route == "degraded_no_key"

    with _session(committing_db) as s:
        row = repository.get_advisory_row(s, tenant="acme", run_date=RUN_DATE)
        assert row is not None
        assert row.degraded is True
        assert row.model_id is None  # no model was called
        task = s.exec(select(AdvisoryTask).where(AdvisoryTask.tenant == "acme")).one()
        assert task.status == "done"
        assert task.advisory_id == row.id  # task links the advisory it produced


# --- S3.6: the router skip path (clear-cut day, key present) -----------------


def test_worker_skips_the_model_on_a_clear_cut_day(committing_db, monkeypatch):
    # Force a key present AND a clear-cut day, so the router (not a missing key) is
    # what skips the model.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")

    from vinea.jobs import router as router_mod

    monkeypatch.setattr(
        worker,
        "route_for",
        lambda features: router_mod.RouteDecision(router_mod.Route.SKIP_MODEL, "forced"),
    )

    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        s.commit()
    with _session(committing_db) as s:
        task = queue.claim_one(s, worker_id="w1")
        # model= matches the key we set, so has_api_key is True and the *router*
        # (not a missing key) is what skips the model.
        result = worker.process_one(s, task, model="openai:gpt-4o-mini")

    assert result.status == "done"
    assert result.route == "skip_model"
    assert result.degraded is False  # complete answer, just no model needed

    with _session(committing_db) as s:
        row = repository.get_advisory_row(s, tenant="acme", run_date=RUN_DATE)
        assert row.degraded is False
        assert row.model_id is None  # skipped, so no model recorded


# --- S3.2 / large model: the full graph path --------------------------------


def test_worker_runs_the_graph_on_a_borderline_day(committing_db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")

    from vinea.jobs import router as router_mod

    monkeypatch.setattr(
        worker, "route_for", lambda f: router_mod.RouteDecision(router_mod.Route.LARGE_MODEL, "forced")
    )

    o_irr, o_spr, o_coord = _grounded_overrides(_features())

    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        s.commit()

    with o_irr, o_spr, o_coord:
        with _session(committing_db) as s:
            task = queue.claim_one(s, worker_id="w1")
            result = worker.process_one(s, task, model="openai:gpt-4o-mini")

    assert result.status == "done"
    assert result.route == "large_model"
    with _session(committing_db) as s:
        row = repository.get_advisory_row(s, tenant="acme", run_date=RUN_DATE)
        assert row.degraded is False
        assert row.model_id == "openai:gpt-4o-mini"  # provenance recorded


def test_worker_stores_the_trace_id_when_tracing_is_configured(committing_db, monkeypatch):
    """S4.3 through the queue: with tracing on, the large-model path stores a
    trace_id on the advisory row -- the deep link S6 will follow."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from vinea.jobs import router as router_mod
    from vinea.obs import tracing

    exporter = InMemorySpanExporter()
    tracing.configure_tracing(processor=SimpleSpanProcessor(exporter), include_content=False)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setattr(
        worker, "route_for", lambda f: router_mod.RouteDecision(router_mod.Route.LARGE_MODEL, "x")
    )

    o_irr, o_spr, o_coord = _grounded_overrides(_features())
    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        s.commit()
    with o_irr, o_spr, o_coord:
        with _session(committing_db) as s:
            task = queue.claim_one(s, worker_id="w1")
            worker.process_one(s, task, model="openai:gpt-4o-mini")

    with _session(committing_db) as s:
        row = repository.get_advisory_row(s, tenant="acme", run_date=RUN_DATE)
        assert row.trace_id is not None and len(row.trace_id) == 32
        exported = {format(sp.context.trace_id, "032x") for sp in exporter.get_finished_spans()}
        assert row.trace_id in exported  # the stored id ties to the real trace
    exporter.clear()


# --- failure path: the worker doesn't die, it re-enqueues -------------------


def test_worker_failure_reenqueues_and_does_not_raise(committing_db, monkeypatch):
    for var in _KEYS:
        monkeypatch.delenv(var, raising=False)

    # Make the degraded builder blow up, to exercise the failure path without a model.
    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(worker, "build_degraded_advisory", boom)

    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE, max_attempts=3)
        s.commit()
    with _session(committing_db) as s:
        task = queue.claim_one(s, worker_id="w1")
        result = worker.process_one(s, task)  # must not raise

    assert result.status.startswith("retry_in_")
    with _session(committing_db) as s:
        task = s.exec(select(AdvisoryTask).where(AdvisoryTask.tenant == "acme")).one()
        assert task.status == "queued"
        assert "synthetic failure" in task.last_error


# --- S3.2: the scheduler + run_worker draining ------------------------------


def test_scheduler_enqueues_one_task_per_active_tenant(committing_db):
    with _session(committing_db) as s:
        repository.save_grower_config(s, Deps(), tenant="t1", location="b", region="eu")
        repository.save_grower_config(s, Deps(), tenant="t2", location="b", region="eu")
        s.commit()

    with _session(committing_db) as s:
        newly = scheduler.enqueue_nightly(s, run_date=RUN_DATE)
    assert set(newly) == {"t1", "t2"}

    # Idempotent: a second run enqueues nothing new.
    with _session(committing_db) as s:
        newly2 = scheduler.enqueue_nightly(s, run_date=RUN_DATE)
    assert newly2 == []


def test_run_worker_drains_the_queue_and_samples_depth(committing_db, monkeypatch):
    for var in _KEYS:
        monkeypatch.delenv(var, raising=False)

    with _session(committing_db) as s:
        scheduler.enqueue_nightly(s, run_date=RUN_DATE, tenants=["a", "b", "c"])

    processed = worker.run_worker(worker_id="w1", engine=committing_db, max_tasks=None)
    assert processed == 3

    with _session(committing_db) as s:
        done = s.exec(select(AdvisoryTask).where(AdvisoryTask.status == "done")).all()
        assert len(done) == 3
        # S3.4: depth was sampled into the DB as the queue drained.
        samples = s.exec(select(QueueDepthSample)).all()
        assert len(samples) == 3


# --- phase 14: cost on the row, and the two ways a gateway says no ----------


def test_cost_from_the_run_lands_on_the_advisory_row(committing_db, monkeypatch):
    """The four columns are written from one object, or not written at all.

    Asserted at the worker rather than in `test_gateway.py` because the claim is
    about persistence: a ledger that fills up and never reaches a row would pass
    every test in the gateway module and answer nobody's question about the night.
    """
    from vinea.gateway.ledger import RunCost
    from vinea.jobs import router as router_mod
    from vinea.obs.instrumented import InstrumentedResult

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setattr(
        worker, "route_for", lambda f: router_mod.RouteDecision(router_mod.Route.LARGE_MODEL, "x")
    )

    features = _features()
    o_irr, o_spr, o_coord = _grounded_overrides(features)
    real_run = worker.run_advisory_instrumented

    def _with_cost(*args, **kwargs):
        result = real_run(*args, **kwargs)
        return InstrumentedResult(
            advisory=result.advisory,
            trace_id=result.trace_id,
            pre_correction_output=result.pre_correction_output,
            retried=result.retried,
            cost=RunCost(input_tokens=3120, output_tokens=284, cost_usd=0.0114, cache_hit=False),
        )

    monkeypatch.setattr(worker, "run_advisory_instrumented", _with_cost)

    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        s.commit()
    with o_irr, o_spr, o_coord:
        with _session(committing_db) as s:
            task = queue.claim_one(s, worker_id="w1")
            worker.process_one(s, task, model="openai:gpt-4o-mini")

    with _session(committing_db) as s:
        row = repository.get_advisory_row(s, tenant="acme", run_date=RUN_DATE)
        assert (row.input_tokens, row.output_tokens) == (3120, 284)
        assert row.cost_usd == pytest.approx(0.0114)
        assert row.cache_hit is False


def test_a_night_with_no_model_call_leaves_the_cost_columns_null(committing_db, monkeypatch):
    """No key -> no call -> four NULLs. Not four zeros.

    Zero would read as "we called the model and it was free", which is the one
    thing that never happens. The distinction matters the moment someone averages
    cost per advisory across a month that contained a degraded night.
    """
    for var in _KEYS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("VINEA_GATEWAY_URL", raising=False)

    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        s.commit()
    with _session(committing_db) as s:
        task = queue.claim_one(s, worker_id="w1")
        result = worker.process_one(s, task)

    assert result.route == "degraded_no_key"
    with _session(committing_db) as s:
        row = repository.get_advisory_row(s, tenant="acme", run_date=RUN_DATE)
        assert row.input_tokens is None
        assert row.output_tokens is None
        assert row.cost_usd is None
        assert row.cache_hit is None


def test_a_budget_refusal_degrades_instead_of_retrying(committing_db, monkeypatch):
    """The distinction the `gateway.budget` module exists for.

    An outage is transient and belongs in the retry machinery -- 02:00 may be
    fine at 02:05. A spend ceiling is not: retrying burns the night's attempts
    against an answer that will not change until a human raises a limit. So the
    refusal ends the model path, the grower gets the deterministic advisory
    honestly flagged `degraded`, and the task completes rather than failing.
    """
    from pydantic_ai.exceptions import ModelHTTPError

    from vinea.jobs import router as router_mod

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setattr(
        worker, "route_for", lambda f: router_mod.RouteDecision(router_mod.Route.LARGE_MODEL, "x")
    )

    def _refuse(*args, **kwargs):
        raise ModelHTTPError(
            status_code=400,
            model_name="vinea-nightly",
            body={"error": {"message": "Budget has been exceeded! max_budget: 10.0"}},
        )

    monkeypatch.setattr(worker, "run_advisory_instrumented", _refuse)

    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        s.commit()
    with _session(committing_db) as s:
        task = queue.claim_one(s, worker_id="w1")
        result = worker.process_one(s, task, model="openai:gpt-4o-mini")

    assert result.status == "done"          # NOT retried
    assert result.route == "budget_refused"
    assert result.degraded is True
    with _session(committing_db) as s:
        row = repository.get_advisory_row(s, tenant="acme", run_date=RUN_DATE)
        assert row.degraded is True
        assert row.model_id is None         # no model produced this
        assert row.cost_usd is None
        task = s.exec(select(AdvisoryTask).where(AdvisoryTask.tenant == "acme")).one()
        assert task.status == "done"


def test_an_outage_is_not_mistaken_for_a_budget_refusal(committing_db, monkeypatch):
    """The other half of the distinction: a 502 still goes to the retry path.

    Getting this backwards is the expensive failure -- every outage would silently
    become a degraded night that looks like a policy decision, and nobody would
    ever be paged for a gateway that is simply down.
    """
    from pydantic_ai.exceptions import ModelHTTPError

    from vinea.jobs import router as router_mod

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setattr(
        worker, "route_for", lambda f: router_mod.RouteDecision(router_mod.Route.LARGE_MODEL, "x")
    )

    def _outage(*args, **kwargs):
        raise ModelHTTPError(status_code=502, model_name="vinea-nightly", body="bad gateway")

    monkeypatch.setattr(worker, "run_advisory_instrumented", _outage)

    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE, max_attempts=3)
        s.commit()
    with _session(committing_db) as s:
        task = queue.claim_one(s, worker_id="w1")
        result = worker.process_one(s, task, model="openai:gpt-4o-mini")

    assert result.status.startswith("retry_in_")
    assert result.route == "error"
