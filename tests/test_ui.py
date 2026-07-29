"""phase 11 (S6) -- the UI: talks only to the API, and the API endpoints it needs.

The Streamlit rendering itself isn't unit-tested (it's a view), but the two things
that matter *are*: the API client's data layer works against a real API, and the UI
package structurally cannot reach the database. That second test is S6's equivalent
of S5.4 -- the rule enforced, not just stated.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import open_ops_session
from vinea.api import main
from vinea.ui.client import ApiClient, langfuse_trace_url

TENANT = "acme"
API_KEY = "key-acme"
OPS_KEY = "ops-secret"
RUN_DATE = date(2025, 2, 8)

UI_DIR = Path(__file__).parent.parent / "src" / "vinea" / "ui"
APP_PATH = str(UI_DIR / "app.py")

# The internals the UI must never reach. If any UI module imports one of these,
# "the UI talks only to the API" has become a slogan.
FORBIDDEN_IMPORTS = {
    "vinea.db",
    "vinea.jobs",
    "vinea.agents",
    "vinea.graph",
    "vinea.features",
    "vinea.reconcile",
    "vinea.obs",
    "sqlmodel",
    "sqlalchemy",
}


# --- THE RULE: the UI reaches the system only through the API ---------------


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_ui_never_imports_the_database_or_internals():
    """S6's rule, enforced by scanning the source: the UI's only door is the API
    client. Any import of db/jobs/agents/graph/sqlmodel here would let the UI bypass
    the API, so this test fails if one appears."""
    offenders: dict[str, set[str]] = {}
    for py in UI_DIR.rglob("*.py"):
        bad = {
            imp
            for imp in _imports_of(py)
            if any(imp == f or imp.startswith(f + ".") for f in FORBIDDEN_IMPORTS)
        }
        if bad:
            offenders[str(py.relative_to(UI_DIR))] = bad
    assert not offenders, f"UI modules reach past the API: {offenders}"


def test_client_only_module_dependency_is_httpx():
    """The client talks HTTP, full stop -- it imports httpx, not a repository."""
    imports = _imports_of(UI_DIR / "client.py")
    assert "httpx" in imports
    assert not any(i.startswith("vinea.db") for i in imports)


# --- the client's data layer, against a real API ----------------------------


@pytest.fixture
def api_client(committing_db, monkeypatch):
    """An ApiClient whose httpx calls are routed into an in-process TestClient.

    We monkeypatch httpx.get/post in the client module to hit the FastAPI
    TestClient instead of a socket, so the whole client<->API path is exercised
    offline, with the real routes, auth, and DB.
    """
    monkeypatch.setenv("VINEA_API_KEYS", f"{API_KEY}:{TENANT}")
    monkeypatch.setenv("VINEA_OPS_KEY", OPS_KEY)
    main.app.dependency_overrides[main.get_engine] = lambda: committing_db
    test_client = TestClient(main.app)

    import vinea.ui.client as client_mod

    def fake_get(url, headers=None, params=None, timeout=None):
        return test_client.get(url.replace("http://testserver", ""), headers=headers, params=params)

    def fake_post(url, headers=None, timeout=None):
        return test_client.post(url.replace("http://testserver", ""), headers=headers)

    monkeypatch.setattr(client_mod.httpx, "get", fake_get)
    monkeypatch.setattr(client_mod.httpx, "post", fake_post)

    yield ApiClient(base_url="http://testserver", tenant_key=API_KEY, ops_key=OPS_KEY)
    main.app.dependency_overrides.clear()


def _seed(engine, *, tenant=TENANT, run_date=RUN_DATE, degraded=True, trace_id=None, confidence=0.6, cost=None):
    from vinea.contracts import DailyFarmAdvisory, IrrigationAdvice, SprayAdvice
    from vinea.db import repository
    from vinea.deps import Deps

    advisory = DailyFarmAdvisory(
        date=date(2025, 2, 9),
        irrigation=IrrigationAdvice(
            target_date=date(2025, 2, 9),
            should_irrigate_tomorrow=True,
            recommended_depth_mm=12.0,
            current_depletion_mm=80.0,
            confidence=confidence,
            rationale="seed",
        ),
        spray=SprayAdvice(
            target_date=date(2025, 2, 9),
            can_spray_tomorrow=False,
            recommended_windows=[],
            limiting_factors=["seed: none"],
            confidence=confidence,
            rationale="seed",
        ),
        summary="seed plan",
        conflicts_resolved=[],
        overall_confidence=confidence,
    )
    with open_ops_session(engine) as s:
        repository.save_advisory(
            s,
            advisory,
            tenant=tenant,
            run_date=run_date,
            deps=Deps(),
            degraded=degraded,
            trace_id=trace_id,
            cost=cost,
        )
        s.commit()


def test_client_health(api_client):
    assert api_client.health()["status"] == "ok"


def test_client_get_advisory_roundtrips(api_client, committing_db):
    _seed(committing_db, trace_id="abc123")
    env = api_client.get_advisory(TENANT, RUN_DATE)
    assert env is not None
    assert env["advisory"]["irrigation"]["current_depletion_mm"] == 80.0
    assert env["trace_id"] == "abc123"


def test_client_missing_advisory_is_none(api_client):
    assert api_client.get_advisory(TENANT, date(1999, 1, 1)) is None


def test_client_enqueue_then_history(api_client, committing_db):
    api_client.enqueue(TENANT, RUN_DATE)
    _seed(committing_db, run_date=date(2025, 2, 7))
    history = api_client.list_advisories(TENANT)
    assert any(row["run_date"] == "2025-02-07" for row in history)


# --- the ops endpoints the operator/quality panels need ---------------------


def test_client_queue_depth_and_history(api_client, committing_db):
    from vinea.jobs import metrics, queue

    with open_ops_session(committing_db) as s:
        queue.enqueue(s, tenant=TENANT, run_date=RUN_DATE)
        metrics.sample_queue_depth(s)
        s.commit()

    assert api_client.queue_depth()["queued"] == 1
    history = api_client.queue_history()
    assert len(history) == 1
    assert history[0]["queued"] == 1


def test_client_all_advisories_is_cross_tenant(api_client, committing_db):
    _seed(committing_db, tenant="acme")
    _seed(committing_db, tenant="olivares")
    rows = api_client.all_advisories()
    tenants = {r["tenant"] for r in rows}
    assert tenants == {"acme", "olivares"}, "the ops feed must span tenants"


def test_ops_endpoints_reject_a_tenant_key(api_client):
    # A client with no ops key can't reach operator surface -- the credential split
    # from S5.3 holds at the client layer too. Version-agnostic on the exception:
    # both TestClient's httpx and the production client raise their own
    # HTTPStatusError; we assert the behaviour (a 401 is raised), not the class.
    api_client.ops_key = None
    with pytest.raises(Exception, match="401"):
        api_client.queue_depth()


# --- the trace deep-link ----------------------------------------------------


def test_langfuse_trace_url_points_at_the_configured_host(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example.com")
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "ai")
    url = langfuse_trace_url("deadbeef")
    assert url == "https://lf.example.com/project/ai/traces/deadbeef"


# --- the panels actually render (AppTest), fully offline --------------------


@pytest.fixture
def app_test_env(committing_db, monkeypatch):
    """Route the Streamlit app's httpx through an in-process TestClient.

    This runs the REAL app.py -- sidebar, panels, charts -- against the real API and
    DB, with no server and no network. It's what catches a rendering bug (a chart
    that can't serialise, a KeyError in a panel) that a "did the server boot" check
    sails past. Seeds two tenants so every panel has data.
    """
    monkeypatch.setenv("VINEA_API_KEYS", f"{API_KEY}:{TENANT},key-olivares:olivares")
    monkeypatch.setenv("VINEA_OPS_KEY", OPS_KEY)
    monkeypatch.setenv("VINEA_API_URL", "http://testserver")
    monkeypatch.setenv("VINEA_UI_TENANT_KEY", API_KEY)
    main.app.dependency_overrides[main.get_engine] = lambda: committing_db
    test_client = TestClient(main.app)

    import vinea.ui.client as client_mod

    monkeypatch.setattr(
        client_mod.httpx,
        "get",
        lambda url, headers=None, params=None, timeout=None: test_client.get(
            url.replace("http://testserver", ""), headers=headers, params=params
        ),
    )
    monkeypatch.setattr(
        client_mod.httpx,
        "post",
        lambda url, headers=None, timeout=None: test_client.post(
            url.replace("http://testserver", ""), headers=headers
        ),
    )

    from vinea.gateway.ledger import RunCost
    from vinea.jobs import metrics, queue

    # One priced night and one unpriced one, so the Cost panel renders both its
    # aggregates and its "no cost recorded" counter rather than only the empty state.
    _seed(
        committing_db,
        tenant=TENANT,
        degraded=False,
        trace_id="abc123",
        confidence=0.8,
        cost=RunCost(input_tokens=3120, output_tokens=284, cost_usd=0.0114, cache_hit=False),
    )
    _seed(committing_db, tenant="olivares", degraded=True, confidence=0.4)
    with open_ops_session(committing_db) as s:
        queue.enqueue(s, tenant=TENANT, run_date=date(2025, 2, 10))
        metrics.sample_queue_depth(s)
        s.commit()

    yield
    main.app.dependency_overrides.clear()


@pytest.mark.parametrize("panel", ["Grower view", "Operator overview", "Quality monitor", "Cost"])
def test_every_panel_renders_without_error(app_test_env, panel):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value(panel).run()

    # at.exception is an ElementList (empty when nothing raised), not None.
    assert not at.exception, f"{panel} raised: {list(at.exception)}"
    # It drew *something* -- a header at minimum, proving the panel executed.
    assert len(at.header) >= 1
