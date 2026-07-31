"""Two tenants, two vineyards, two advisories.

The worker used to load one weather file for every tenant. With a single demo site
that is invisible; with two it is wrong -- identical advice for vineyards several
hundred kilometres apart, produced confidently.

`weather_observations` has been keyed by `(tenant, location)` since it was created
and `sources/persist.py` always wrote it correctly. Nothing read it back, because
nothing knew where a tenant was.

These tests run the real worker on the real degraded path -- no model, no network --
so what they compare is the physics reacting to different inputs.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path

import pytest
from sqlmodel import select

from tests.conftest import open_ops_session
from vinea.db import repository
from vinea.db.models import WeatherObservation
from vinea.deps import WINE_GRAPES
from vinea.ingest import load_weather
from vinea.jobs import queue, worker
from vinea.sources.db_source import API_SOURCE
from vinea.sources.persist import upsert_observations

pytestmark = pytest.mark.db

RUN_DATE = date(2026, 7, 28)

# Two real Greek wine regions, real coordinates. Not placeholders: the point is
# that a continental northern site and a Peloponnesian one genuinely differ in
# July, so the advisories differing is physics rather than an artefact.
NEMEA = (37.8125, 22.6875)
NAOUSSA = (40.6300, 22.0700)


def _seed_config(session, tenant: str, coordinates: tuple[float, float] | None) -> None:
    latitude, longitude = coordinates if coordinates else (None, None)
    row = repository.save_grower_config(
        session, WINE_GRAPES, tenant=tenant, location="block-a", region="eu"
    )
    row.latitude = latitude
    row.longitude = longitude
    session.add(row)
    session.flush()


@lru_cache(maxsize=1)
def _committed_rows():
    """Parsed once per session: six seedings of the same two 888-row files is the
    kind of repeated I/O that turns a fast suite into a slow one."""
    data_dir = Path("data")
    history, forecast, _ = load_weather(
        sorted(data_dir.glob("*last-30d*.csv"))[-1],
        sorted(data_dir.glob("*next-7d*.csv"))[-1],
        RUN_DATE,
    )
    return history, forecast


def _seed_weather(session, tenant: str, *, et0_scale: float) -> None:
    """Persist the committed capture for one tenant, with ET0 scaled.

    Scaling ET0 is the cleanest way to make two tenants genuinely different in the
    quantity the advisory turns on: cumulative ETc drives depletion, which drives
    the irrigation decision. Everything else about the two series stays identical,
    so a difference in the advisories can only have come from this.
    """
    history, forecast = _committed_rows()

    def scaled(rows):
        out = []
        for row in rows:
            if row.et0_mm is None:
                out.append(row)
            else:
                out.append(row.model_copy(update={"et0_mm": row.et0_mm * et0_scale}))
        return out

    for kind, rows in (("history", scaled(history)), ("forecast", scaled(forecast))):
        upsert_observations(
            session, rows, tenant=tenant, location="block-a", kind=kind, source=API_SOURCE
        )


@pytest.fixture
def two_tenants(committing_db, monkeypatch):
    """Two configured tenants with different stored weather, and no network.

    `_refresh_observations` is stubbed to a no-op: the fetch path has its own tests,
    and letting these reach the real Open-Meteo API would make them slow, flaky and
    dependent on today's weather in Naoussa.
    """
    monkeypatch.setattr(worker, "_refresh_observations", lambda *a, **k: None)

    with open_ops_session(committing_db) as session:
        _seed_config(session, "nemea", NEMEA)
        _seed_config(session, "naoussa", NAOUSSA)
        _seed_weather(session, "nemea", et0_scale=1.0)
        _seed_weather(session, "naoussa", et0_scale=0.45)  # cooler, wetter north
        session.commit()
    return committing_db


def _run(engine, tenant: str):
    with open_ops_session(engine) as session:
        queue.enqueue(session, tenant=tenant, run_date=RUN_DATE)
        session.commit()
    with open_ops_session(engine) as session:
        task = queue.claim_one(session, worker_id="w1")
        return worker.process_one(session, task)


def test_two_tenants_get_different_advisories(two_tenants, monkeypatch):
    """The whole point of F1, on the deterministic path.

    No API key, so no model runs and both advisories come straight from
    `features.py`. If they match, the worker is still reading one file for
    everybody.
    """
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("VINEA_GATEWAY_URL", raising=False)

    assert _run(two_tenants, "nemea").status == "done"
    assert _run(two_tenants, "naoussa").status == "done"

    with open_ops_session(two_tenants) as session:
        nemea = repository.get_advisory(session, tenant="nemea", run_date=RUN_DATE)
        naoussa = repository.get_advisory(session, tenant="naoussa", run_date=RUN_DATE)

    assert nemea is not None and naoussa is not None
    nemea_depletion = nemea.irrigation.current_depletion_mm
    naoussa_depletion = naoussa.irrigation.current_depletion_mm

    assert nemea_depletion != naoussa_depletion, (
        "both tenants got the same depletion -- the worker is reading one weather "
        f"source for everybody ({nemea_depletion} mm)"
    )
    # And in the direction the physics demands: less ET0 means less depletion.
    assert naoussa_depletion < nemea_depletion


def test_each_tenant_reads_only_its_own_observations(two_tenants):
    """Isolation at the weather layer, not only at the advisory layer.

    `weather_observations` is one of the tables row-level security polices, and the
    read is by `(tenant, location)` as well -- belt and braces, because a leak here
    would silently give one grower another's numbers rather than another's advice.
    """
    from vinea.sources.db_source import DbSource

    with open_ops_session(two_tenants) as session:
        nemea = DbSource(session, tenant="nemea", location="block-a").load(run_date=RUN_DATE)
        naoussa = DbSource(session, tenant="naoussa", location="block-a").load(run_date=RUN_DATE)

    assert nemea.history and naoussa.history
    nemea_et0 = sum(r.et0_mm for r in nemea.history if r.et0_mm is not None)
    naoussa_et0 = sum(r.et0_mm for r in naoussa.history if r.et0_mm is not None)
    assert nemea_et0 > naoussa_et0 * 1.5, (nemea_et0, naoussa_et0)


def test_a_tenant_without_coordinates_falls_back_and_says_so(committing_db, monkeypatch):
    """A newly created tenant produces an advisory on its first night.

    The alternative -- skipping or erroring -- would make onboarding a tenant a
    two-night process, and a silently omitted tenant is worse than one whose
    advisory says where its numbers came from.
    """
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("VINEA_GATEWAY_URL", raising=False)

    with open_ops_session(committing_db) as session:
        _seed_config(session, "brandnew", None)
        session.commit()

    assert _run(committing_db, "brandnew").status == "done"

    with open_ops_session(committing_db) as session:
        advisory = repository.get_advisory(session, tenant="brandnew", run_date=RUN_DATE)
    assert advisory is not None
    # On the summary, not only on DataQuality. A caveat is rendered from the
    # confidence *penalty*, and "these numbers are from somewhere else" carries no
    # penalty -- the data may be perfectly good, it is simply not this block's. The
    # first version of this recorded the note and never showed it.
    assert "no coordinates configured" in advisory.summary, advisory.summary
    assert "[source —" in advisory.summary


def test_a_failed_fetch_uses_stored_rows_rather_than_failing_the_night(two_tenants, monkeypatch):
    """A provider outage at 02:00 is not a failed night.

    There is almost certainly yesterday's data in the table; the advisory built from
    it carries a staleness penalty and the grower gets real physics on slightly old
    numbers. The note is how that reaches the advisory instead of only a log.
    """
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("VINEA_GATEWAY_URL", raising=False)

    def _outage(*_args, **_kwargs):
        return "weather feed unreachable (ConnectError); using stored observations"

    monkeypatch.setattr(worker, "_refresh_observations", _outage)

    result = _run(two_tenants, "nemea")
    assert result.status == "done", "a feed outage must not fail the night"

    with open_ops_session(two_tenants) as session:
        advisory = repository.get_advisory(session, tenant="nemea", run_date=RUN_DATE)
    assert advisory is not None
    assert advisory.irrigation.current_depletion_mm > 0, "stored rows were not used"


def test_observations_are_written_for_the_right_tenant_and_window(two_tenants):
    """The natural key is doing its job: same hour, two tenants, two rows."""
    midnight = datetime.combine(RUN_DATE, time.min)
    with open_ops_session(two_tenants) as session:
        rows = session.exec(
            select(WeatherObservation.tenant, WeatherObservation.kind)
            .where(
                WeatherObservation.source == API_SOURCE,
                WeatherObservation.observed_at >= midnight - timedelta(days=30),
            )
        ).all()
    tenants = {t for t, _ in rows}
    kinds = {k for _, k in rows}
    assert tenants == {"nemea", "naoussa"}, tenants
    assert kinds == {"history", "forecast"}, kinds
