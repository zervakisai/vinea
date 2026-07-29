"""API response models. The existing contracts, plus the provenance around them.

The advisory itself is `DailyFarmAdvisory` -- the same contract the graph produces,
the repository stores, and the tests hand-check. The API does not define a second
advisory shape; that would fork the contract the way a second mapping layer would
(phase 6). What the API *does* add is a thin envelope for the things that live *beside*
the advisory on the row -- trace_id, degraded, the prompt/model provenance --
because a client viewing an advisory wants the deep link and the "was this
degraded" badge, and those are about the advisory, not part of it (the same
row-vs-contract split `get_advisory` and `get_advisory_row` make).
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from vinea.contracts import DailyFarmAdvisory
from vinea.db.models import Advisory, QueueDepthSample


class EnqueueResponse(BaseModel):
    """202 body for POST: the task was accepted, here's how to track it.

    Deliberately NOT an advisory -- POST returns before the advisory exists,
    because producing it is the worker's job. Returning a task handle rather than a
    result is the API telling the truth about what just happened: work was queued,
    not done.
    """

    task_id: int
    tenant: str
    run_date: date
    status: str
    already_queued: bool


class CitationOut(BaseModel):
    """One corpus passage that was **shown to** the model for one leg.

    Not "used by". `advisory_citations` records what retrieval supplied, because
    asking a model which sources it used is a self-report and phase 12 exists to
    establish that self-report is not evidence. Any client rendering these must
    label them accordingly -- the difference is the whole epistemic content.
    """

    leg: str
    locator: str
    rank: int


class AdvisoryEnvelope(BaseModel):
    """An advisory plus the provenance beside it, for GET.

    `advisory` is the untouched contract; the rest are the row's columns the UI
    needs -- the degraded badge, the confidence numbers (promoted to columns in phase 6
    for exactly this kind of read), and the trace_id deep link.
    """

    tenant: str
    run_date: date
    advisory: DailyFarmAdvisory
    degraded: bool
    trace_id: str | None
    model_id: str | None
    prompt_version: str | None
    overall_confidence: float | None
    # phase 15. Additive and defaulted, so an older client ignores a field it does
    # not know and a night with no corpus ingested simply carries an empty list --
    # which is the fail-open floor surfacing honestly rather than as an error.
    citations: list[CitationOut] = []

    @classmethod
    def from_row(
        cls, row: Advisory, advisory: DailyFarmAdvisory, citations: list | None = None
    ) -> AdvisoryEnvelope:
        return cls(
            tenant=row.tenant,
            run_date=row.run_date,
            advisory=advisory,
            degraded=row.degraded,
            trace_id=row.trace_id,
            model_id=row.model_id,
            prompt_version=row.prompt_version,
            overall_confidence=row.overall_confidence,
            citations=[
                CitationOut(leg=c.leg, locator=c.locator, rank=c.rank) for c in (citations or [])
            ],
        )


class AdvisorySummary(BaseModel):
    """One row in a history listing -- the headline, not the whole advisory.

    History is a list view: a client wants dates, confidence, and flags to scan,
    then fetches the full advisory for the one it cares about. Shipping every full
    advisory in the list would be a lot of JSONB for a scroll.
    """

    tenant: str
    run_date: date
    target_date: date
    degraded: bool
    overall_confidence: float | None
    trace_id: str | None

    # phase 14. Additive and optional: an older client ignores four fields it
    # does not know, which is the same property the expand migration relies on
    # one layer down. All None for a night that called no model, or one that ran
    # without a gateway to report cost -- never 0.0, which would read as free.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cache_hit: bool | None = None

    @classmethod
    def from_row(cls, row: Advisory) -> AdvisorySummary:
        return cls(
            tenant=row.tenant,
            run_date=row.run_date,
            target_date=row.target_date,
            degraded=row.degraded,
            overall_confidence=row.overall_confidence,
            trace_id=row.trace_id,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_usd=row.cost_usd,
            cache_hit=row.cache_hit,
        )


class QueueDepthResponse(BaseModel):
    """GET /ops/queue: the counts an operator (and an autoscaler) act on."""

    queued: int
    running: int
    done: int
    failed: int


class QueueDepthPoint(BaseModel):
    """One point on the queue-depth-over-time chart (S6.3)."""

    sampled_at: datetime
    queued: int
    running: int
    done: int
    failed: int

    @classmethod
    def from_row(cls, row: QueueDepthSample) -> QueueDepthPoint:
        return cls(
            sampled_at=row.sampled_at,
            queued=row.queued,
            running=row.running,
            done=row.done,
            failed=row.failed,
        )


class SLOStatus(BaseModel):
    """One objective, its measurement, and what the budget says to do.

    `value` and `met` are both nullable, together. An objective that is written
    down but not collected -- read latency, as things stand (ADR-010) -- reports
    null rather than a plausible number, because "no advisories were late" and
    "we could not tell" look identical on a dashboard and mean opposite things.
    """

    key: str
    description: str
    target: float
    unit: str
    window_days: int
    value: float | None
    met: bool | None
    sample_size: int
    budget_allowed: float | None = None
    budget_observed: int | None = None
    budget_remaining: float | None = None
    budget_exhausted: bool | None = None
    policy: str | None = None


class HealthResponse(BaseModel):
    status: str
    database: str
