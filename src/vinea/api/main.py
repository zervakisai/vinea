"""The FastAPI app. Thin by rule: it enqueues and reads, it never runs a model.

Every route here does one of two things: write a queue row (`POST`), or read the
database (`GET`). None of them import an agent, build features, or run the graph --
that work belongs to the worker (phase 8), reached through the queue. The proof is
S5.4's test: a `POST` succeeds with `ALLOW_MODEL_REQUESTS=False`, which would raise
if any model were touched, and returns a 202 with a task id before an advisory
exists.

Routes:
  GET  /health                              liveness + DB reachability
  POST /advisories/{tenant}/{run_date}      enqueue; 202 + task handle
  GET  /advisories/{tenant}/{run_date}      one advisory + provenance
  GET  /advisories/{tenant}?from=&to=       history summaries
  GET  /ops/queue                           queue depth (ops key)
  GET  /ops/queue/history                   queue depth over time (ops key)
  GET  /ops/advisories                      cross-tenant summaries (ops key)
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import Engine, text
from sqlmodel import Session

from vinea.api import auth
from vinea.api.schemas import (
    AdvisoryEnvelope,
    AdvisorySummary,
    EnqueueResponse,
    HealthResponse,
    QueueDepthPoint,
    QueueDepthResponse,
)
from vinea.db import repository
from vinea.db.session import make_engine
from vinea.jobs import metrics, queue

# A single engine for the app's lifetime; sessions are per-request. Overridable in
# tests via dependency_overrides on `get_engine`.
_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session(engine: Engine = Depends(get_engine)) -> Iterator[Session]:
    """One session per request. The route owns the transaction (commits on write)."""
    with Session(engine) as session:
        yield session


app = FastAPI(
    title="Vinea Advisory API",
    summary="A thin layer over the advisory queue and store. It enqueues and reads; it never runs a model.",
    version="0.1.0",
)


@app.get("/health", response_model=HealthResponse)
def health(session: Session = Depends(get_session)) -> HealthResponse:
    """Liveness + a real DB round trip. Unauthenticated by design."""
    try:
        session.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # noqa: BLE001 -- health must report, not raise
        database = "unreachable"
    return HealthResponse(status="ok", database=database)


@app.post(
    "/advisories/{tenant}/{run_date}",
    response_model=EnqueueResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_advisory(
    run_date: date,
    tenant: str = Depends(auth.scoped_tenant),
    session: Session = Depends(get_session),
) -> EnqueueResponse:
    """Enqueue the advisory for (tenant, run_date). Returns 202, NOT the advisory.

    THE RULE, in one function: this enqueues a task and returns. It does not run
    the graph, call a model, or compute features -- the worker does that, later,
    off the queue. 202 (Accepted, not 200 OK) is the honest status: the request was
    accepted for processing that hasn't happened yet.

    Idempotent on (tenant, run_date): a second POST for the same night returns the
    existing task rather than creating a duplicate (S3.2's enqueue), so a client
    retrying a POST can't fan out a night into many advisories.
    """
    existing = _existing_task(session, tenant=tenant, run_date=run_date)
    task = queue.enqueue(session, tenant=tenant, run_date=run_date)
    session.commit()
    return EnqueueResponse(
        task_id=task.id,
        tenant=tenant,
        run_date=run_date,
        status=task.status,
        already_queued=existing,
    )


@app.get("/advisories/{tenant}/{run_date}", response_model=AdvisoryEnvelope)
def get_advisory(
    run_date: date,
    tenant: str = Depends(auth.scoped_tenant),
    session: Session = Depends(get_session),
) -> AdvisoryEnvelope:
    """One advisory + its provenance, or 404 if it hasn't been produced yet.

    404, not 202-still-waiting: the advisory either exists in the store or it
    doesn't. Whether a task is queued is a separate question the client asked with
    POST and can poll with the task handle; this endpoint is about the result.
    """
    row = repository.get_advisory_row(session, tenant=tenant, run_date=run_date)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No advisory for tenant '{tenant}' on {run_date.isoformat()}.",
        )
    advisory = repository.get_advisory(session, tenant=tenant, run_date=run_date)
    return AdvisoryEnvelope.from_row(row, advisory)


@app.get("/advisories/{tenant}", response_model=list[AdvisorySummary])
def list_advisories(
    tenant: str = Depends(auth.scoped_tenant),
    session: Session = Depends(get_session),
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
) -> list[AdvisorySummary]:
    """History for a tenant, newest first, as summaries (not full advisories)."""
    rows = repository.list_advisory_rows(session, tenant=tenant, start=from_, end=to)
    return [AdvisorySummary.from_row(r) for r in rows]


@app.get("/ops/queue", response_model=QueueDepthResponse, dependencies=[Depends(auth.require_ops_key)])
def ops_queue(session: Session = Depends(get_session)) -> QueueDepthResponse:
    """Queue depth across all tenants. Ops-key gated -- it spans tenants."""
    depth = metrics.current_depth(session)
    return QueueDepthResponse(**depth)


@app.get(
    "/ops/queue/history",
    response_model=list[QueueDepthPoint],
    dependencies=[Depends(auth.require_ops_key)],
)
def ops_queue_history(
    session: Session = Depends(get_session),
    limit: int = Query(default=500, le=5000),
) -> list[QueueDepthPoint]:
    """The queue-depth time series S6.3 charts. Newest first, ops-key gated.

    Reads `queue_depth_samples` (S3.4) -- the whole reason those snapshots are
    stored rather than recomputed: `current_depth` gives you now, this gives you
    the history an autoscaler tunes against.
    """
    samples = metrics.depth_over_time(session, limit=limit)
    return [QueueDepthPoint.from_row(s) for s in samples]


@app.get(
    "/ops/advisories",
    response_model=list[AdvisorySummary],
    dependencies=[Depends(auth.require_ops_key)],
)
def ops_advisories(
    session: Session = Depends(get_session),
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    limit: int = Query(default=500, le=5000),
) -> list[AdvisorySummary]:
    """Advisory summaries across ALL tenants -- the operator quality monitor's feed.

    Ops-key gated because it's cross-tenant (S6.4 computes degraded rate and
    confidence distribution over everyone). The UI does the aggregation client-side
    from these summaries; the API just serves the rows.
    """
    rows = repository.list_all_advisory_rows(session, start=from_, end=to, limit=limit)
    return [AdvisorySummary.from_row(r) for r in rows]


def _existing_task(session: Session, *, tenant: str, run_date: date) -> bool:
    from sqlmodel import select

    from vinea.db.models import AdvisoryTask

    return (
        session.exec(
            select(AdvisoryTask.id).where(
                AdvisoryTask.tenant == tenant, AdvisoryTask.run_date == run_date
            )
        ).first()
        is not None
    )
