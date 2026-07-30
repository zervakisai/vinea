"""The FastAPI app. Thin by rule: it enqueues and reads, it never runs a model.

Every route here does one of two things: write a queue row (`POST`), or read the
database (`GET`). None of them import an agent, build features, or run the graph --
that work belongs to the worker, reached through the queue. The proof is
a test: a `POST` succeeds with `ALLOW_MODEL_REQUESTS=False`, which would raise
if any model were touched, and returns a 202 with a task id before an advisory
exists.

Routes:
  GET  /health                              liveness: is this process answering?
  GET  /ready                               readiness: can it serve? 503 if not
  POST /advisories/{tenant}/{run_date}      enqueue; 202 + task handle
  GET  /advisories/{tenant}/{run_date}      one advisory + provenance
  GET  /advisories/{tenant}?from=&to=       history summaries
  GET  /ops/queue                           queue depth (ops key)
  GET  /ops/queue/history                   queue depth over time (ops key)
  GET  /ops/advisories                      cross-tenant summaries (ops key)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
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
    SLOStatus,
)
from vinea.db import repository
from vinea.db.models import ApiRequestSample
from vinea.db.session import make_engine, scope_to_ops, scope_to_tenant
from vinea.jobs import metrics, queue

logger = logging.getLogger(__name__)

# A single engine for the app's lifetime; sessions are per-request. Overridable in
# tests via dependency_overrides on `get_engine`.
_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session(engine: Engine = Depends(get_engine)) -> Iterator[Session]:
    """One session per request. The route owns the transaction (commits on write).

    Deliberately UNSCOPED, and under row-level security that means it sees nothing:
    every connection runs as `vinea_app`, and the row policies filter everything
    when no tenant is declared. Only the probes use it -- they run `SELECT 1`,
    which touches no tenant table.

    Routes take `tenant_session` or `ops_session` instead, so "which rows may
    this request see?" is answered by which dependency it declares rather than
    by whether its query remembered a WHERE clause.
    """
    with Session(engine) as session:
        yield session


def tenant_session(
    tenant: str = Depends(auth.scoped_tenant),
    engine: Engine = Depends(get_engine),
) -> Iterator[Session]:
    """A session that can see exactly one tenant's rows, and no others.

    Composes authentication (the API key), authorization (the key owns this
    tenant) and *enforcement* (the database will not return anything else) into
    one dependency. The third is the one that matters most, and the
    third is the one that survives a future query written by someone who has
    never read `auth.py`.
    """
    with Session(engine) as session:
        scope_to_tenant(session, tenant)
        yield session


def ops_session(
    engine: Engine = Depends(get_engine),
    _: None = Depends(auth.require_ops_key),
) -> Iterator[Session]:
    """A session that may see every tenant. Gated on the ops key, never a tenant key.

    The ops key check is a *dependency of the session itself* rather than of the
    route, so a cross-tenant view cannot be opened without it. Declaring the two
    separately on a route would let someone add a route with the session and
    forget the key.
    """
    with Session(engine) as session:
        scope_to_ops(session)
        yield session


app = FastAPI(
    title="Vinea Advisory API",
    summary="A thin layer over the advisory queue and store. It enqueues and reads; it never runs a model.",
    version="0.2.0",
)

# Routes whose latency is an SLO. Only these are timed.
#
# NOT /health and /ready: Kubernetes probes them every few seconds, which is
# thousands of rows a day of nothing anyone promised, drowning the few hundred
# requests a grower actually makes. An SLI measured over probe traffic reports the
# health of the probe.
TIMED_ROUTES = frozenset({"/advisories/{tenant}/{run_date}", "/advisories/{tenant}"})


@app.middleware("http")
async def record_request_latency(request, call_next):
    """Time the grower-facing reads and store the timing. Never fails a request.

    A synchronous insert in the read path, which is a real cost stated plainly:
    it is affordable because this route is served a few hundred times a day, and
    it would not be at a thousand requests a second. The alternative -- an
    in-process histogram -- dies with the pod and cannot be aggregated across
    replicas, which is exactly why ADR-010 left this objective uncollected.

    `route` is the path *template* from the matched route, never the resolved
    path: resolved paths would make every tenant its own series and put tenant
    names in a table with no row policy on it.
    """
    import time

    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000.0

    matched = request.scope.get("route")
    route = getattr(matched, "path", "") or ""
    if request.method == "GET" and route in TIMED_ROUTES:
        try:
            with Session(get_engine()) as session:
                scope_to_ops(session)
                session.add(
                    ApiRequestSample(
                        route=route,
                        method=request.method,
                        status_code=response.status_code,
                        duration_ms=duration_ms,
                    )
                )
                session.commit()
        except Exception:  # noqa: BLE001 -- measurement must never break the thing measured
            logger.debug("could not record request latency for %s", route, exc_info=True)

    return response


def _database_state(session: Session) -> str:
    try:
        session.execute(text("SELECT 1"))
        return "ok"
    except Exception:  # noqa: BLE001 -- health must report, not raise
        return "unreachable"


@app.get("/health", response_model=HealthResponse)
def health(session: Session = Depends(get_session)) -> HealthResponse:
    """Liveness + a real DB round trip. Unauthenticated by design.

    Always 200, on purpose: this answers "is this process alive?", and the answer
    does not change when Postgres goes away. A liveness probe that fails on an
    unreachable database restarts every pod in the deployment, repeatedly, for a
    fault that no restart can fix -- turning a database outage into a crash loop
    that is *harder* to recover from. The database state is reported in the body
    instead, where a human or a body-reading healthcheck can act on it.
    """
    return HealthResponse(status="ok", database=_database_state(session))


@app.get(
    "/ready",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "Not ready to serve"}},
)
def ready(response: Response, session: Session = Depends(get_session)) -> HealthResponse:
    """Readiness: should this pod receive traffic? 503 when it should not.

    The counterpart to /health, and a genuinely different question. Every route
    other than /health reads the database, so a pod that cannot reach Postgres
    cannot serve -- it should be taken out of the load balancer, but NOT
    restarted. Kubernetes draws exactly that line: readiness removes endpoints,
    liveness kills containers.

    It has to be a status code rather than a field, because an httpGet probe reads
    the status and nothing else. That is also why /health alone was not enough:
    `curl -f /health` passes with no database at all.
    """
    database = _database_state(session)
    if database != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="degraded", database=database)
    return HealthResponse(status="ok", database=database)


@app.post(
    "/advisories/{tenant}/{run_date}",
    response_model=EnqueueResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_advisory(
    run_date: date,
    tenant: str = Depends(auth.scoped_tenant),
    session: Session = Depends(tenant_session),
) -> EnqueueResponse:
    """Enqueue the advisory for (tenant, run_date). Returns 202, NOT the advisory.

    THE RULE, in one function: this enqueues a task and returns. It does not run
    the graph, call a model, or compute features -- the worker does that, later,
    off the queue. 202 (Accepted, not 200 OK) is the honest status: the request was
    accepted for processing that hasn't happened yet.

    Idempotent on (tenant, run_date): a second POST for the same night returns the
    existing task rather than creating a duplicate, so a client
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
    session: Session = Depends(tenant_session),
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
    citations = repository.get_citations(session, advisory_id=row.id)
    return AdvisoryEnvelope.from_row(row, advisory, citations)


@app.get("/advisories/{tenant}", response_model=list[AdvisorySummary])
def list_advisories(
    tenant: str = Depends(auth.scoped_tenant),
    session: Session = Depends(tenant_session),
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
) -> list[AdvisorySummary]:
    """History for a tenant, newest first, as summaries (not full advisories)."""
    rows = repository.list_advisory_rows(session, tenant=tenant, start=from_, end=to)
    return [AdvisorySummary.from_row(r) for r in rows]


@app.get("/ops/queue", response_model=QueueDepthResponse)
def ops_queue(session: Session = Depends(ops_session)) -> QueueDepthResponse:
    """Queue depth across all tenants. Ops-key gated -- it spans tenants."""
    depth = metrics.current_depth(session)
    return QueueDepthResponse(**depth)


@app.get(
    "/ops/queue/history",
    response_model=list[QueueDepthPoint],
)
def ops_queue_history(
    session: Session = Depends(ops_session),
    limit: int = Query(default=500, le=5000),
) -> list[QueueDepthPoint]:
    """The queue-depth time series the operator panel charts. Newest first, ops-key gated.

    Reads `queue_depth_samples` -- the whole reason those snapshots are
    stored rather than recomputed: `current_depth` gives you now, this gives you
    the history an autoscaler tunes against.
    """
    samples = metrics.depth_over_time(session, limit=limit)
    return [QueueDepthPoint.from_row(s) for s in samples]


@app.get(
    "/ops/advisories",
    response_model=list[AdvisorySummary],
)
def ops_advisories(
    session: Session = Depends(ops_session),
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    limit: int = Query(default=500, le=5000),
) -> list[AdvisorySummary]:
    """Advisory summaries across ALL tenants -- the operator quality monitor's feed.

    Ops-key gated because it's cross-tenant (the quality monitor computes degraded rate and
    confidence distribution over everyone). The UI does the aggregation client-side
    from these summaries; the API just serves the rows.
    """
    rows = repository.list_all_advisory_rows(session, start=from_, end=to, limit=limit)
    return [AdvisorySummary.from_row(r) for r in rows]


@app.get("/ops/slo", response_model=list[SLOStatus])
def ops_slo(
    session: Session = Depends(ops_session),
    today: date | None = Query(default=None),
) -> list[SLOStatus]:
    """The three objectives, measured. Ops-gated because SLIs are cross-tenant.

    Served from the API rather than a `/metrics` scrape target, for the reason
    ADR-010 gives: nothing in this deployment scrapes, and an endpoint whose
    format assumes a collector that does not exist is a format nobody reads. The
    UI reaches it through `ApiClient` like every other panel (ADR-005).
    """
    from vinea.slo import error_budget, measure_all

    results = measure_all(session, today=today or date.today())
    out: list[SLOStatus] = []
    for result in results:
        budget = error_budget(result)
        out.append(
            SLOStatus(
                key=result.objective.key,
                description=result.objective.description,
                target=result.objective.target,
                unit=result.objective.unit,
                window_days=result.objective.window_days,
                value=result.value,
                met=result.met,
                sample_size=result.sample_size,
                budget_allowed=budget.allowed_failures if budget else None,
                budget_observed=budget.observed_failures if budget else None,
                budget_remaining=budget.remaining if budget else None,
                budget_exhausted=budget.exhausted if budget else None,
                policy=budget.policy if budget else None,
            )
        )
    return out


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
