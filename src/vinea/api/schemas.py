"""API response models: the domain contracts, plus the provenance around them.

The advisory itself is `DailyFarmAdvisory` — the same contract the graph produces,
the repository stores and the tests check. The API defines no second advisory
shape; that would fork the contract the way a second mapping layer forks a schema.

What it adds is a thin envelope for the things that live *beside* the advisory on
its row — trace id, the degraded flag, model and prompt provenance, cost. A client
viewing an advisory wants the deep link and the badge, and those are facts about
the advisory rather than part of it. `repository.get_advisory` and
`get_advisory_row` draw the same line one layer down.
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
    asking a model which sources it used is a self-report, and self-report is not
    evidence. Any client rendering these must
    label them accordingly -- the difference is the whole epistemic content.
    """

    leg: str
    locator: str
    rank: int


class AdvisoryEnvelope(BaseModel):
    """An advisory plus the provenance beside it, for GET.

    `advisory` is the untouched contract; the rest are the row's columns the UI
    needs -- the degraded badge, the confidence numbers (promoted to columns for
    exactly this kind of read), and the trace_id deep link.
    """

    tenant: str
    run_date: date
    advisory: DailyFarmAdvisory
    degraded: bool
    trace_id: str | None
    model_id: str | None
    prompt_version: str | None
    overall_confidence: float | None
    # Additive and defaulted, so an older client ignores a field it does
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

    # Additive and optional: an older client ignores four fields it
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
    """One point on the queue-depth-over-time chart."""

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


class AnnotationCreate(BaseModel):
    """What a reviewer submits about one advisory.

    No `advisory_id`: the advisory is addressed by the URL (tenant + run_date),
    and accepting an id in the body would let a caller annotate an advisory the
    path -- and therefore the auth check -- never looked at.

    `reviewer_id` is who is speaking, as a label ("maria", "agronomist-on-call").
    It is not authenticated identity -- the API key authenticates the *tenant* --
    and pretending otherwise would be claiming a property the system does not
    have. ADR-012's revisit trigger (an identity provider) is where that changes.
    """

    reviewer_role: str = Field(pattern="^(agronomist|farmer)$")
    reviewer_id: str = Field(min_length=1, max_length=120)
    verdict: str = Field(pattern="^(agree|disagree|unclear)$")
    # None means "about the advisory as a whole" -- the normal case for a farmer
    # judging the summary they actually read.
    leg: str | None = Field(default=None, pattern="^(irrigation|spray|reconciliation)$")
    comment: str | None = Field(default=None, max_length=2000)


class AnnotationRead(BaseModel):
    """One recorded judgement, as the API returns it."""

    id: int
    reviewer_role: str
    reviewer_id: str
    verdict: str
    leg: str | None
    comment: str | None
    promoted_to_golden: bool
    created_at: datetime

    @classmethod
    def from_row(cls, row) -> AnnotationRead:
        return cls(
            id=row.id,
            reviewer_role=str(row.reviewer_role),
            reviewer_id=row.reviewer_id,
            verdict=row.verdict,
            leg=row.leg,
            comment=row.comment,
            promoted_to_golden=row.promoted_to_golden,
            created_at=row.created_at,
        )
