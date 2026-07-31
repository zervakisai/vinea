"""The worker: claim a task, produce the advisory, persist it, own the retry.

A worker is a stateless loop (ADR-003): claim a task with SKIP LOCKED, do the
work, write the result, mark the task -- and hold nothing in memory that isn't
also on a row, so a second worker can take over the instant this one dies.

`process_one` is the whole per-task story and is written to be called directly by
a test with a real database and a mocked model. `run_worker` is the thin loop
around it.

Three ways a task's advisory gets produced, and the worker chooses among them
without the graph or the agents knowing:

  1. No API key -> `build_degraded_advisory`, `degraded=True`. The grower gets the
     deterministic answer.
  2. Router says the day is clear-cut -> `build_degraded_advisory`,
     `degraded=False` but it just didn't need a model. Not "degraded" --
     the answer is complete.
  3. Otherwise -> the full graph, `run_advisory_sync`.

The instrumented runner is what records the trace id, the pre-correction attempt
and the cost; the plain graph call records none of them. Every one of those columns
is nullable for that reason -- a column that arrives empty is honest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlmodel import Session, select

from vinea import config
from vinea.db import repository
from vinea.db.models import AdvisoryTask, GrowerConfig
from vinea.db.session import make_engine, scope_to_ops
from vinea.deps import WINE_GRAPES, Deps
from vinea.features import build_features
from vinea.gateway import is_budget_refusal
from vinea.gateway.ledger import RunCost
from vinea.ingest import WeatherLoadResult
from vinea.jobs import metrics, queue
from vinea.jobs.degraded import build_degraded_advisory
from vinea.jobs.router import Route, route_for
from vinea.obs.instrumented import run_advisory_instrumented
from vinea.sources.csv_source import CsvSource

logger = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    task_id: int
    status: str  # 'done' | 'failed' | 'retry_in_...'
    # 'degraded_no_key' | 'skip_model' | 'large_model' | 'budget_refused' | 'error'
    route: str
    degraded: bool


def _primary_location(session: Session, tenant: str) -> str:
    loc = session.exec(
        select(GrowerConfig.location)
        .where(GrowerConfig.tenant == tenant, GrowerConfig.valid_to.is_(None))
        .order_by(GrowerConfig.location)
    ).first()
    return loc or "default"


def _coordinates(session: Session, *, tenant: str, location: str) -> tuple[float, float] | None:
    """Where this block is, or None if nobody said.

    Read from the same open config row the thresholds come from, so a tenant is
    configured in one place. None means the tenant predates the columns or was
    seeded without them, and the caller falls back to the bundled capture with a
    note -- not to a guessed coordinate, which would place a grower's vineyard
    somewhere it is not.
    """
    row = session.exec(
        select(GrowerConfig.latitude, GrowerConfig.longitude).where(
            GrowerConfig.tenant == tenant,
            GrowerConfig.location == location,
            GrowerConfig.valid_to.is_(None),
        )
    ).first()
    if row is None:
        return None
    latitude, longitude = row
    if latitude is None or longitude is None:
        return None
    return float(latitude), float(longitude)


def _resolve_deps(session: Session, *, tenant: str) -> Deps:
    """The Deps for a tenant's block, from grower_config, or the default.

    This is where "new crop = config change, not code change" cashes out in the
    batch path: the worker reads the tenant's configured thresholds from the
    database rather than importing WINE_GRAPES. Falls back to the default only when
    a tenant has no config row yet, so the demo and the tests work without seeding
    config first.
    """
    deps = repository.get_current_deps(
        session, tenant=tenant, location=_primary_location(session, tenant)
    )
    return deps or WINE_GRAPES


HISTORY_DAYS = 30
FORECAST_DAYS = 7

# How long the nightly fetch may take per tenant before the worker gives up and
# uses what is already stored. Generous next to an HTTP call and small next to the
# task deadline: ten tenants timing out still leaves the batch inside its window.
FETCH_TIMEOUT_SECONDS = 20.0


def _refresh_observations(
    session: Session, *, tenant: str, location: str, coordinates: tuple[float, float], run_date: date
) -> str | None:
    """Fetch this block's weather and persist it. Returns a note on failure, else None.

    Fetch, persist, and let the caller read back from the database. The indirection
    is what makes a retry cheap: a task retried three times would otherwise hit the
    provider three times for hours it already has, and rate limits are a worse
    failure than staleness.

    Never raises. A provider outage at 02:00 must not fail a night -- there is
    almost certainly yesterday's data in the table, the advisory built from it
    carries a staleness penalty, and the grower gets real physics on slightly old
    numbers. That is the correct degrade, and the note is how it reaches the
    advisory instead of only a log line.
    """
    import httpx

    from vinea.sources.db_source import API_SOURCE
    from vinea.sources.open_meteo import OpenMeteoSource
    from vinea.sources.persist import upsert_observations

    latitude, longitude = coordinates
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS) as client:
            fetched = OpenMeteoSource(client=client).load(
                latitude=latitude,
                longitude=longitude,
                history_days=HISTORY_DAYS,
                forecast_days=FORECAST_DAYS,
                run_date=run_date,
            )
    except Exception as exc:  # noqa: BLE001 -- a feed outage is not a failed night
        logger.warning(
            "weather fetch failed for %s/%s: %s: %s", tenant, location, type(exc).__name__, exc
        )
        return f"weather feed unreachable ({type(exc).__name__}); using stored observations"

    for kind, rows in (("history", fetched.history), ("forecast", fetched.forecast)):
        upsert_observations(
            session, rows, tenant=tenant, location=location, kind=kind, source=API_SOURCE
        )
    return None


def _load_weather(
    session: Session,
    *,
    tenant: str,
    location: str,
    run_date: date,
) -> tuple[WeatherLoadResult, tuple[str, ...]]:
    """This block's weather, and any notes about where it came from.

    Three rungs, and which one ran is visible on the advisory rather than only in a
    log:

      1. **Coordinates configured** -> refresh from the provider, then read
         `weather_observations` for this exact (tenant, location).
      2. **Fetch failed but rows exist** -> read them anyway. Staleness lowers
         confidence on its own; there is no need to fail a night over it.
      3. **No coordinates, or nothing stored** -> the bundled capture, with a note
         saying so.

    Rung 3 is why the demo keeps working and why a newly-created tenant produces an
    advisory on its first night instead of an error. It is also the rung that used
    to be the *only* one, which meant every tenant was advised from one vineyard's
    weather.
    """
    from vinea.sources.db_source import DbSource

    notes: list[str] = []
    coordinates = _coordinates(session, tenant=tenant, location=location)

    if coordinates is not None:
        note = _refresh_observations(
            session, tenant=tenant, location=location, coordinates=coordinates, run_date=run_date
        )
        if note:
            notes.append(note)

        db_source = DbSource(
            session,
            tenant=tenant,
            location=location,
            staleness_threshold_hours=config.STALENESS_THRESHOLD_HOURS,
        )
        if db_source.has_rows(run_date=run_date, history_days=HISTORY_DAYS):
            return db_source.load(run_date=run_date, history_days=HISTORY_DAYS,
                                  forecast_days=FORECAST_DAYS), tuple(notes)
        notes.append("no stored observations for this block; using the bundled capture")
    else:
        notes.append("no coordinates configured for this block; using the bundled capture")

    data_dir = config.DEFAULT_DATA_DIR
    history = sorted(Path(data_dir).glob("*last-30d*.csv"))[-1]
    forecast = sorted(Path(data_dir).glob("*next-7d*.csv"))[-1]
    result = CsvSource(
        history, forecast, staleness_threshold_hours=config.STALENESS_THRESHOLD_HOURS
    ).load(run_date=run_date)
    return result, tuple(notes)


def process_one(
    session: Session,
    task: AdvisoryTask,
    *,
    model: str = config.MODEL,
) -> ProcessResult:
    """Produce and persist the advisory for one already-claimed task.

    The caller has claimed `task` (status 'running', attempts incremented). This
    function does the work and marks the task done, or -- on any exception -- hands
    the retry decision to the single owner in `queue.mark_failed_or_retry`. It
    never retries on its own; that's the footgun this whole design avoids.
    """
    try:
        deps = _resolve_deps(session, tenant=task.tenant)
        location = _primary_location(session, task.tenant)
        load_result, source_notes = _load_weather(
            session, tenant=task.tenant, location=location, run_date=task.run_date
        )
        if source_notes:
            # Onto the quality verdict, which is what the agents read.
            load_result = WeatherLoadResult(
                history=load_result.history,
                forecast=load_result.forecast,
                quality=load_result.quality.model_copy(
                    update={"notes": [*load_result.quality.notes, *source_notes]}
                ),
            )
        features = build_features(
            list(load_result.history),
            list(load_result.forecast),
            load_result.quality,
            task.run_date,
            deps,
        )

        # --- the three-way choice, made from features + config, no model yet ---
        trace_id: str | None = None
        pre_correction: dict | None = None
        cost = RunCost(input_tokens=None, output_tokens=None, cost_usd=None, cache_hit=None)
        passages: list = []

        if not config.has_api_key(model):
            advisory = build_degraded_advisory(features, list(load_result.forecast), deps)
            route, degraded, model_id = "degraded_no_key", True, None
        else:
            decision = route_for(features)
            if decision.route is Route.SKIP_MODEL:
                advisory = build_degraded_advisory(features, list(load_result.forecast), deps)
                route, degraded, model_id = "skip_model", False, None
            else:
                # The instrumented runner produces the same advisory as the plain
                # graph, plus the trace id and the pre-correction attempt.
                # It's a no-op wrapper when tracing is off, so the worker
                # always uses it -- pre-correction capture doesn't need OTel, and
                # trace_id is simply None without it.
                try:
                    instrumented = run_advisory_instrumented(
                        load_result, deps, model=model, tenant=task.tenant, run_date=task.run_date
                    )
                except Exception as exc:  # noqa: BLE001 -- re-raised unless it is a budget refusal
                    # The gateway being *unreachable* never reaches here:
                    # FallbackModel already tried the direct provider, and if there
                    # was none, the exception below is an outage and falls through
                    # to the retry machinery, which is correct -- an outage at 02:00
                    # may be over at 02:05.
                    #
                    # A *budget refusal* is the opposite: retrying it burns the
                    # night's attempts against an answer that will not change until
                    # a human raises a limit. So it terminates the model path here
                    # and the grower gets the deterministic advisory -- real physics,
                    # honestly flagged -- instead of an error or three more refusals.
                    if not is_budget_refusal(exc):
                        raise
                    advisory = build_degraded_advisory(
                        features, list(load_result.forecast), deps
                    )
                    route, degraded, model_id = "budget_refused", True, None
                else:
                    advisory = instrumented.advisory
                    trace_id = instrumented.trace_id
                    pre_correction = instrumented.pre_correction_output
                    cost = instrumented.cost
                    passages = instrumented.passages
                    route, degraded, model_id = "large_model", False, model

        if source_notes:
            # And onto the advisory itself, which is the only place a grower looks.
            #
            # Putting them only on `DataQuality` was not enough: a caveat is
            # rendered from the confidence *penalty*, and "the numbers came from
            # somewhere else" carries no penalty -- the data may be perfectly good,
            # it is simply not this block's. So the note was recorded and never
            # shown, which is the failure mode this whole change exists to fix.
            advisory = advisory.model_copy(
                update={"summary": advisory.summary + "\n[source — " + "; ".join(source_notes) + "]"}
            )

        # Persist advisory and mark the task done in ONE transaction, so the two
        # land together (queue.mark_done does not commit).
        saved = repository.save_advisory(
            session,
            advisory,
            tenant=task.tenant,
            run_date=task.run_date,
            deps=deps,
            model_id=model_id,
            degraded=degraded,
            trace_id=trace_id,
            pre_correction_output=pre_correction,
            cost=cost,
        )
        # Citations land in the SAME transaction as the advisory and the task.
        # A crash between them would leave an advisory that claims grounding it
        # cannot show, or citations pointing at an advisory that was rolled back.
        repository.save_citations(session, advisory_id=saved.id, passages=passages)
        queue.mark_done(session, task, advisory_id=saved.id)
        session.commit()
        return ProcessResult(task_id=task.id, status="done", route=route, degraded=degraded)

    except Exception as exc:  # noqa: BLE001 -- the worker's job is to not die
        # A failed attempt must not leave a half-written advisory in this session.
        session.rollback()
        session.refresh(task)
        decision = queue.mark_failed_or_retry(session, task, error=f"{type(exc).__name__}: {exc}")
        return ProcessResult(task_id=task.id, status=decision, route="error", degraded=False)


def run_worker(
    *,
    worker_id: str,
    engine=None,
    max_tasks: int | None = None,
    sample_metrics: bool = True,
) -> int:
    """Claim-and-process until the queue is empty (or `max_tasks` reached).

    Returns the number of tasks processed. `max_tasks` bounds a single run -- a
    nightly worker sets it to None and drains the queue; a test sets it to a small
    number. Each task gets its own claim commit and its own process/commit, so a
    crash between tasks loses at most the one in flight, which the reaper recovers.
    """
    engine = engine or make_engine()
    processed = 0

    while max_tasks is None or processed < max_tasks:
        with Session(engine) as session:
            # The worker is legitimately cross-tenant: it claims from one queue
            # spanning every tenant with SKIP LOCKED, which is the whole point of
            # ADR-003's design. Under RLS that needs the escape declared once,
            # here, rather than a policy exception per table.
            scope_to_ops(session)
            task = queue.claim_one(session, worker_id=worker_id)
            if task is None:
                break  # queue drained
            process_one(session, task)
            if sample_metrics:
                metrics.sample_queue_depth(session)
                session.commit()
            processed += 1

    return processed
