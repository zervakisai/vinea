"""phase 10 (S5) -- the thin API: enqueue and read, never run a model.

Every test here runs with ALLOW_MODEL_REQUESTS=False (conftest sets it), so a
route that touched a model would raise loudly. That's not incidental -- it's
S5.4's proof mechanism: the POST works precisely because it never reaches an agent.

These use `committing_db` because the API commits its writes (a POST that enqueues
has to be visible to a worker on another connection), and a FastAPI dependency
override points the app at the test engine.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from vinea.api import main
from vinea.db.models import Advisory, AdvisoryTask
from vinea.deps import Deps

pytestmark = pytest.mark.db

TENANT = "acme"
API_KEY = "key-acme"
OTHER_KEY = "key-olivares"
RUN_DATE = date(2025, 2, 8)


@pytest.fixture
def client(committing_db, monkeypatch):
    """A TestClient wired to the test engine, with two tenants' keys set."""
    monkeypatch.setenv("VINEA_API_KEYS", f"{API_KEY}:{TENANT},{OTHER_KEY}:olivares")
    monkeypatch.setenv("VINEA_OPS_KEY", "ops-secret")
    main.app.dependency_overrides[main.get_engine] = lambda: committing_db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _auth(key: str = API_KEY) -> dict[str, str]:
    return {"X-API-Key": key}


# --- S5.1: health + auth ----------------------------------------------------


def test_health_is_unauthenticated_and_checks_the_db(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok"}


def test_missing_or_bad_key_is_401(client):
    assert client.post(f"/advisories/{TENANT}/{RUN_DATE}").status_code == 401
    assert (
        client.post(f"/advisories/{TENANT}/{RUN_DATE}", headers={"X-API-Key": "nope"}).status_code
        == 401
    )


def test_a_key_cannot_reach_another_tenant(client):
    # olivares's key against acme's path -> 403. Authentication succeeded (valid
    # key), authorization failed (wrong tenant). That distinction is the whole
    # point of per-tenant keys.
    r = client.post(f"/advisories/{TENANT}/{RUN_DATE}", headers=_auth(OTHER_KEY))
    assert r.status_code == 403


# --- S5.4: THE RULE -- POST enqueues and returns BEFORE any model runs -------


def test_post_returns_202_with_a_task_handle_not_an_advisory(client):
    r = client.post(f"/advisories/{TENANT}/{RUN_DATE}", headers=_auth())
    assert r.status_code == 202  # Accepted, not 200 OK
    body = r.json()
    assert body["tenant"] == TENANT
    assert body["status"] == "queued"
    assert isinstance(body["task_id"], int)
    # It returned a task handle, not an advisory. The advisory doesn't exist yet.
    assert "irrigation" not in body


def test_post_enqueues_a_task_and_creates_no_advisory(client, committing_db):
    """The proof that the API ran no model: after POST, a queued task exists and NO
    advisory does -- producing one is the worker's job, and this whole test runs
    under ALLOW_MODEL_REQUESTS=False, so any model call would have raised."""
    client.post(f"/advisories/{TENANT}/{RUN_DATE}", headers=_auth())

    with Session(committing_db) as s:
        task = s.exec(select(AdvisoryTask).where(AdvisoryTask.tenant == TENANT)).one()
        assert task.status == "queued"
        advisories = s.exec(select(Advisory).where(Advisory.tenant == TENANT)).all()
        assert advisories == [], "the API must not have produced an advisory inline"


def test_post_is_idempotent_on_a_re_post(client):
    first = client.post(f"/advisories/{TENANT}/{RUN_DATE}", headers=_auth()).json()
    second = client.post(f"/advisories/{TENANT}/{RUN_DATE}", headers=_auth()).json()
    assert first["task_id"] == second["task_id"]
    assert second["already_queued"] is True


# --- S5.2: GET advisory + history -------------------------------------------


def _seed_advisory(engine, *, tenant=TENANT, run_date=RUN_DATE, degraded=True, trace_id=None):
    """Write an advisory straight to the DB, as the worker would."""
    from vinea.contracts import DailyFarmAdvisory, IrrigationAdvice, SprayAdvice
    from vinea.db import repository

    advisory = DailyFarmAdvisory(
        date=date(2025, 2, 9),
        irrigation=IrrigationAdvice(
            target_date=date(2025, 2, 9),
            should_irrigate_tomorrow=True,
            recommended_depth_mm=12.0,
            current_depletion_mm=80.0,
            confidence=0.7,
            rationale="seed",
        ),
        spray=SprayAdvice(
            target_date=date(2025, 2, 9),
            can_spray_tomorrow=False,
            recommended_windows=[],
            limiting_factors=["seed: none"],
            confidence=0.6,
            rationale="seed",
        ),
        summary="seed plan",
        conflicts_resolved=[],
        overall_confidence=0.6,
    )
    with Session(engine) as s:
        repository.save_advisory(
            s,
            advisory,
            tenant=tenant,
            run_date=run_date,
            deps=Deps(),
            degraded=degraded,
            trace_id=trace_id,
        )
        s.commit()


def test_get_advisory_returns_the_contract_plus_provenance(client, committing_db):
    _seed_advisory(committing_db, degraded=True, trace_id="abc123")
    r = client.get(f"/advisories/{TENANT}/{RUN_DATE}", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    # The advisory is the untouched contract...
    assert body["advisory"]["irrigation"]["current_depletion_mm"] == 80.0
    # ...and the provenance rides alongside it, not inside it.
    assert body["degraded"] is True
    assert body["trace_id"] == "abc123"
    assert body["overall_confidence"] == pytest.approx(0.6)


def test_get_missing_advisory_is_404(client):
    r = client.get(f"/advisories/{TENANT}/1999-01-01", headers=_auth())
    assert r.status_code == 404


def test_history_lists_summaries_newest_first(client, committing_db):
    for day in (5, 6, 7):
        _seed_advisory(committing_db, run_date=date(2025, 2, day))
    r = client.get(f"/advisories/{TENANT}", headers=_auth())
    assert r.status_code == 200
    dates = [row["run_date"] for row in r.json()]
    assert dates == ["2025-02-07", "2025-02-06", "2025-02-05"]
    # Summaries, not full advisories.
    assert "advisory" not in r.json()[0]


def test_history_can_be_windowed(client, committing_db):
    for day in (5, 6, 7, 8):
        _seed_advisory(committing_db, run_date=date(2025, 2, day))
    r = client.get(f"/advisories/{TENANT}?from=2025-02-06&to=2025-02-07", headers=_auth())
    assert [row["run_date"] for row in r.json()] == ["2025-02-07", "2025-02-06"]


# --- S5.3: /ops/queue -------------------------------------------------------


def test_ops_queue_requires_the_ops_key(client):
    assert client.get("/ops/queue").status_code == 401
    assert client.get("/ops/queue", headers=_auth()).status_code == 401  # tenant key is wrong creds
    r = client.get("/ops/queue", headers={"X-Ops-Key": "ops-secret"})
    assert r.status_code == 200


def test_ops_queue_reports_depth(client, committing_db):
    from vinea.jobs import queue as q

    with Session(committing_db) as s:
        q.enqueue(s, tenant=TENANT, run_date=RUN_DATE)
        q.enqueue(s, tenant="olivares", run_date=RUN_DATE)
        s.commit()
    r = client.get("/ops/queue", headers={"X-Ops-Key": "ops-secret"})
    assert r.json()["queued"] == 2
