"""The SLIs, as SQL over rows the system already keeps.

Every query here is cross-tenant, so every caller needs the phase-17 ops scope.
That is stated rather than assumed because an SLI that silently measured one
tenant would be the most plausible-looking wrong number this system could
produce.

## The deadline that is not the deadline

`advisory_tasks.deadline_at` exists and is *not* what these queries use. It is
enqueue-time + 30 minutes, and it is named precisely: how long the worker keeps
retrying before giving up. That is a question about the worker's patience.

The SLO asks a different question -- when does the grower need this? -- and the
answer is a wall-clock time in their morning. The two can disagree in the
direction that matters: enqueue at 05:50 because the scheduler was late, and the
task gives up at 06:20 having never once violated its deadline, while the grower
had nothing at 06:00.

So availability is measured from `advisories.created_at` against 06:00 in
`grower_config.timezone`, and `deadline_at` keeps doing its own, different job.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import text
from sqlmodel import Session

from vinea.slo.objectives import (
    AVAILABILITY,
    JUDGEMENT_RATE,
    OBJECTIVES,
    READ_LATENCY,
    Objective,
    SLIResult,
)

# The hour a grower needs tomorrow's advisory by, in their own morning.
ADVISORY_DEADLINE_HOUR = 6

# What a tenant with no recorded timezone is judged against. UTC, and the choice
# is visible in the result rather than hidden: a tenant in Chile judged against a
# UTC morning is being held to a deadline four hours earlier than its own, which
# will show up as a breach and send someone to look at the config -- which is the
# correct outcome. Silently skipping such tenants would make the number better and
# the system less honest.
DEFAULT_TIMEZONE = "UTC"

_AVAILABILITY_SQL = text("""
WITH expected AS (
    -- One row per tenant per day the tenant had an open config. This is the
    -- denominator, and getting it from CONFIG rather than from advisories is the
    -- whole point: counting only days that produced an advisory would score
    -- 100% on a night the scheduler never ran.
    SELECT DISTINCT g.tenant,
           COALESCE(g.timezone, :default_tz) AS tz,
           d::date AS run_date
    FROM grower_config g
    -- CAST(...) rather than `:start_date::date`: SQLAlchemy's `text()` parser
    -- mis-reads a bind parameter immediately followed by a `::` cast and leaves
    -- the placeholder in the statement, which reaches Postgres as a syntax error
    -- at a colon. Explicit CAST is unambiguous and reads better anyway.
    CROSS JOIN generate_series(CAST(:start_date AS date), CAST(:end_date AS date), interval '1 day') AS d
    WHERE g.valid_to IS NULL
),
delivered AS (
    SELECT a.tenant,
           a.run_date,
           a.created_at
    FROM advisories a
    WHERE a.run_date BETWEEN :start_date AND :end_date
)
SELECT count(*)                                                        AS expected_count,
       count(*) FILTER (
           WHERE d.created_at IS NOT NULL
             AND d.created_at AT TIME ZONE e.tz
                 < ((e.run_date + 1) + (:deadline_hour * interval '1 hour'))
       )                                                               AS on_time_count
FROM expected e
LEFT JOIN delivered d ON d.tenant = e.tenant AND d.run_date = e.run_date
""")

_DEGRADED_SQL = text("""
SELECT count(*) AS total,
       count(*) FILTER (WHERE degraded) AS degraded_count
FROM advisories
WHERE run_date BETWEEN :start_date AND :end_date
""")


def availability_by_day(
    session: Session, *, today: date, window_days: int = AVAILABILITY.window_days
) -> SLIResult:
    """Fraction of tenant-days whose advisory existed by 06:00 local.

    `created_at AT TIME ZONE tz` converts the stored `timestamptz` into the
    grower's wall clock, and it is compared against 06:00 on the day *after*
    `run_date` -- because an advisory for run_date N advises about N+1, and the
    grower reads it on the morning of N+1.

    Off-by-one risk acknowledged: this is the kind of arithmetic that is wrong in
    a way nobody notices for a year, which is why `tests/test_slo.py` seeds
    advisories on both sides of the boundary rather than only inside it.
    """
    start = today - timedelta(days=window_days)
    row = session.execute(
        _AVAILABILITY_SQL,
        {
            "start_date": start,
            "end_date": today,
            "deadline_hour": ADVISORY_DEADLINE_HOUR,
            "default_tz": DEFAULT_TIMEZONE,
        },
    ).one()
    expected, on_time = int(row[0]), int(row[1])
    value = (on_time / expected) if expected else None
    return SLIResult(objective=AVAILABILITY, value=value, sample_size=expected)


def degraded_rate(
    session: Session, *, today: date, window_days: int = JUDGEMENT_RATE.window_days
) -> SLIResult:
    """Fraction of advisories produced without a model.

    Not an error rate. `degraded=true` is a correct outcome, built on purpose, and
    a grower gets real physics from it -- so this can never page. It is an SLO
    because a fleet running on the deterministic path for a month is correct,
    useful, and not the product that was built.
    """
    start = today - timedelta(days=window_days)
    row = session.execute(_DEGRADED_SQL, {"start_date": start, "end_date": today}).one()
    total, degraded = int(row[0]), int(row[1])
    value = (degraded / total) if total else None
    return SLIResult(objective=JUDGEMENT_RATE, value=value, sample_size=total)


_LATENCY_SQL = text("""
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
       count(*) AS samples
FROM api_request_samples
WHERE observed_at >= now() - make_interval(days => :window_days)
  AND route = :route
  AND method = 'GET'
  AND status_code < 500
""")

# The route the SLO is about: the advisory a grower opens. Not the history list,
# and not the enqueue -- POST returns 202 without waiting for work, so its latency
# is a queue property rather than something anyone experiences.
SLO_READ_ROUTE = "/advisories/{tenant}/{run_date}"


def read_latency_p95(
    session: Session, *, window_days: int = READ_LATENCY.window_days, **_kwargs
) -> SLIResult:
    """p95 of the grower-facing read, from stored timings.

    `percentile_cont`, not a histogram bucket. Exact, and affordable because the
    route is served a few hundred times a day rather than a few hundred times a
    second -- the traffic profile is what makes the simple thing correct here.

    5xx responses are excluded. A request that failed did not take a measurable
    amount of time to succeed, and letting fast errors pull the percentile down is
    how a latency SLI reports health during an outage.

    No samples yields `None`, not 0.0. `Objective.is_met(None)` is None, so an
    idle window cannot be mistaken for an excellent one.
    """
    row = session.execute(
        _LATENCY_SQL, {"window_days": window_days, "route": SLO_READ_ROUTE}
    ).one()
    p95, samples = row[0], int(row[1])
    return SLIResult(
        objective=READ_LATENCY,
        value=float(p95) if p95 is not None else None,
        sample_size=samples,
    )


_MEASURERS = {
    AVAILABILITY.key: availability_by_day,
    JUDGEMENT_RATE.key: degraded_rate,
    READ_LATENCY.key: read_latency_p95,
}


def measure_all(session: Session, *, today: date) -> list[SLIResult]:
    """Every objective, measured or honestly absent. Needs the ops scope."""
    results = []
    for objective in OBJECTIVES:
        measurer = _MEASURERS[objective.key]
        results.append(measurer(session, today=today, window_days=objective.window_days))
    return results


def objective_for(key: str) -> Objective:
    return next(o for o in OBJECTIVES if o.key == key)
