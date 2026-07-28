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
     deterministic answer (S3.5).
  2. Router says the day is clear-cut -> `build_degraded_advisory`,
     `degraded=False` but it just didn't need a model (S3.6). Not "degraded" --
     the answer is complete.
  3. Otherwise -> the full graph, `run_advisory_sync` (S3.2's actual work).

phase 9 will swap the plain graph call for an instrumented one (trace_id +
pre-correction capture); until then this worker records neither, and that's honest
-- the columns are nullable for exactly this reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlmodel import Session, select

from vinea import config
from vinea.db import repository
from vinea.db.models import AdvisoryTask, GrowerConfig
from vinea.db.session import make_engine
from vinea.deps import WINE_GRAPES, Deps
from vinea.features import build_features
from vinea.ingest import WeatherLoadResult
from vinea.jobs import metrics, queue
from vinea.jobs.degraded import build_degraded_advisory
from vinea.jobs.router import Route, route_for
from vinea.obs.instrumented import run_advisory_instrumented
from vinea.sources.csv_source import CsvSource


@dataclass
class ProcessResult:
    task_id: int
    status: str  # 'done' | 'failed' | 'retry_in_...'
    route: str  # 'degraded_no_key' | 'skip_model' | 'large_model' | 'error'
    degraded: bool


def _primary_location(session: Session, tenant: str) -> str:
    loc = session.exec(
        select(GrowerConfig.location)
        .where(GrowerConfig.tenant == tenant, GrowerConfig.valid_to.is_(None))
        .order_by(GrowerConfig.location)
    ).first()
    return loc or "default"


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


def _load_weather(run_date: date) -> WeatherLoadResult:
    """Load this tenant's weather for the run.

    phase 8 uses the bundled CSV fixtures as the batch's weather source, so the queue is
    exercisable end to end offline. A production worker would read
    `weather_observations` for the tenant (populated by S2's `--source api`); that
    swap is a one-function change behind the same WeatherLoadResult, exactly the
    seam phase 7 built. Kept as CSV here so phase 8's tests never need a live feed.
    """
    data_dir = config.DEFAULT_DATA_DIR
    history = sorted(Path(data_dir).glob("*last-30d*.csv"))[-1]
    forecast = sorted(Path(data_dir).glob("*next-7d*.csv"))[-1]
    return CsvSource(
        history, forecast, staleness_threshold_hours=config.STALENESS_THRESHOLD_HOURS
    ).load(run_date=run_date)


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
        load_result = _load_weather(task.run_date)
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
                # graph, plus the trace id (S4.3) and the pre-correction attempt
                # (S4.4). It's a no-op wrapper when tracing is off, so the worker
                # always uses it -- pre-correction capture doesn't need OTel, and
                # trace_id is simply None without it.
                instrumented = run_advisory_instrumented(
                    load_result, deps, model=model, tenant=task.tenant, run_date=task.run_date
                )
                advisory = instrumented.advisory
                trace_id = instrumented.trace_id
                pre_correction = instrumented.pre_correction_output
                route, degraded, model_id = "large_model", False, model

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
        )
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
            task = queue.claim_one(session, worker_id=worker_id)
            if task is None:
                break  # queue drained
            process_one(session, task)
            if sample_metrics:
                metrics.sample_queue_depth(session)
                session.commit()
            processed += 1

    return processed
