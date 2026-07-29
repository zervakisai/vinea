"""phase 18 -- the objectives, the arithmetic, and the boundary that is easy to get wrong.

Every test here seeds rows on **both sides** of a boundary rather than only
inside it. Availability arithmetic involving a timezone and a next-day 06:00
deadline is exactly the kind of thing that is wrong in a direction nobody notices
for a year, and a test that only seeds on-time advisories passes for a query that
has no deadline logic at all.

All of these need the phase-17 ops scope: an SLI is cross-tenant by definition,
and one that silently measured a single tenant would be the most plausible-looking
wrong number this system could produce.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import Session

from vinea.db.session import scope_to_ops
from vinea.slo import error_budget, measure_all
from vinea.slo.objectives import AVAILABILITY, JUDGEMENT_RATE, READ_LATENCY, SLIResult
from vinea.slo.queries import availability_by_day, degraded_rate, read_latency_p95

pytestmark = pytest.mark.db

TODAY = date(2026, 7, 28)


def _config(session, tenant: str, tz: str | None) -> None:
    session.execute(
        text(
            "INSERT INTO grower_config (tenant, location, region, timezone, crop, "
            "irrigation_method, spray_sensitivity, kc, root_depth_m, taw_mm, mad_fraction, "
            "initial_depletion_mm, effective_rain_fraction, rain_skip_mm, refill_fraction, "
            "deltat_ideal_low, deltat_ideal_high, deltat_inversion_below, deltat_marginal_upper, "
            "wind_ideal_low_ms, wind_ideal_high_ms, spray_index_cutoff, "
            "spray_index_higher_is_better, rain_fast_hours, deps_hash) "
            "VALUES (:t, 'block-a', 'eu', :tz, 'vines', 'drip', 'high', 0.7, 1.0, 120, 0.4, "
            "0, 0.8, 2, 0.9, 2, 8, 2, 10, 0.83, 4.2, 0.5, false, 2, 'h')"
        ),
        {"t": tenant, "tz": tz},
    )


def _advisory(session, tenant: str, run_date: date, created_at: datetime, degraded: bool = False) -> None:
    session.execute(
        text(
            "INSERT INTO advisories (tenant, run_date, target_date, irrigation, spray, "
            "reconciliation, deps_hash, degraded, created_at) "
            "VALUES (:t, :d, :d, '{}', '{}', '{}', 'h', :g, :c)"
        ),
        {"t": tenant, "d": run_date, "g": degraded, "c": created_at},
    )


# --------------------------------------------------------------------------- #
# Availability: the boundary, from both sides                                 #
# --------------------------------------------------------------------------- #


def test_an_advisory_that_lands_before_the_local_morning_counts(committing_db):
    """05:59 Athens on the morning the grower reads it. On time."""
    with Session(committing_db) as s:
        scope_to_ops(s)
        _config(s, "nemea", "Europe/Athens")
        # 02:59 UTC == 05:59 in Athens (UTC+3 in July), on the target day.
        _advisory(s, "nemea", TODAY, datetime(2026, 7, 29, 2, 59, tzinfo=UTC))
        s.commit()
        result = availability_by_day(s, today=TODAY, window_days=0)
    assert result.sample_size == 1
    assert result.value == 1.0
    assert result.met is True


def test_an_advisory_that_lands_after_the_local_morning_does_not(committing_db):
    """06:01 Athens. One minute late is late.

    The pair of tests is the point: a query with no deadline logic at all passes
    the previous test and fails this one.
    """
    with Session(committing_db) as s:
        scope_to_ops(s)
        _config(s, "nemea", "Europe/Athens")
        _advisory(s, "nemea", TODAY, datetime(2026, 7, 29, 3, 1, tzinfo=UTC))
        s.commit()
        result = availability_by_day(s, today=TODAY, window_days=0)
    assert result.value == 0.0
    assert result.met is False


def test_the_deadline_is_local_not_utc(committing_db):
    """The whole reason `grower_config.timezone` exists.

    The same instant is on time for one grower and late for another. Judged in
    UTC, both would score identically -- and the Athens grower would be held to a
    deadline three hours before their own morning.
    """
    # 04:30 UTC in July: 07:30 in Athens (EEST, UTC+3) -- late; 05:30 in London
    # (BST, UTC+1) -- on time.
    #
    # The first version of this test used 05:30 UTC and asserted the same split,
    # which is wrong: London is BST in July, so 05:30 UTC is 06:30 local and both
    # tenants are late. The query was right and the test data was not -- which is
    # exactly the class of error that makes offsets unusable and IANA zone names
    # necessary.
    instant = datetime(2026, 7, 29, 4, 30, tzinfo=UTC)
    with Session(committing_db) as s:
        scope_to_ops(s)
        _config(s, "nemea", "Europe/Athens")
        _config(s, "sussex", "Europe/London")
        _advisory(s, "nemea", TODAY, instant)
        _advisory(s, "sussex", TODAY, instant)
        s.commit()
        result = availability_by_day(s, today=TODAY, window_days=0)
    assert result.sample_size == 2
    assert result.value == 0.5, "the same instant must be late in Athens and on time in London"


def test_a_night_that_never_ran_scores_zero_not_one_hundred(committing_db):
    """The denominator comes from CONFIG, not from advisories.

    Counting only days that produced an advisory would score a perfect 100% on a
    night the scheduler never fired -- the single most dangerous way to compute
    an availability SLI, because the failure makes the number *better*.
    """
    with Session(committing_db) as s:
        scope_to_ops(s)
        _config(s, "nemea", "Europe/Athens")
        s.commit()  # a configured tenant, and no advisory at all
        result = availability_by_day(s, today=TODAY, window_days=0)
    assert result.sample_size == 1
    assert result.value == 0.0


def test_a_tenant_with_no_timezone_is_judged_against_utc_and_not_skipped(committing_db):
    """Being wrong loudly beats being absent quietly.

    A tenant with no recorded zone is held to a UTC morning, which for anyone
    east of Greenwich is stricter than their own. That shows up as a breach and
    sends somebody to fix the config. Excluding them would improve the number and
    hide the gap.
    """
    with Session(committing_db) as s:
        scope_to_ops(s)
        _config(s, "unknown-tz", None)
        _advisory(s, "unknown-tz", TODAY, datetime(2026, 7, 29, 7, 0, tzinfo=UTC))
        s.commit()
        result = availability_by_day(s, today=TODAY, window_days=0)
    assert result.sample_size == 1, "a tenant with no timezone must still be counted"
    assert result.value == 0.0


# --------------------------------------------------------------------------- #
# Judgement rate                                                              #
# --------------------------------------------------------------------------- #


def test_degraded_rate_counts_the_deterministic_path(committing_db):
    """Not an error rate. `degraded=true` is a correct outcome phase 8 built on
    purpose; this measures whether the *interesting* half of the system is
    working, which is why it is an SLO and never an alert."""
    with Session(committing_db) as s:
        scope_to_ops(s)
        _config(s, "nemea", "Europe/Athens")
        for offset, degraded in enumerate([True, False, False, False]):
            _advisory(
                s, "nemea", TODAY - timedelta(days=offset),
                datetime(2026, 7, 29, 2, 0, tzinfo=UTC), degraded=degraded,
            )
        s.commit()
        result = degraded_rate(s, today=TODAY, window_days=7)
    assert result.sample_size == 4
    assert result.value == 0.25
    assert result.met is False, "25% degraded must breach a 5% objective"


# --------------------------------------------------------------------------- #
# Honest absence                                                              #
# --------------------------------------------------------------------------- #


def test_an_unmeasured_objective_never_reports_success():
    """The distinction that matters most on a dashboard.

    "No advisories were late" and "we could not tell whether any were late" look
    identical if an absent SLI defaults to a pass. `is_met(None)` is None, not
    True, and nothing downstream can round it up.
    """
    result = SLIResult(objective=AVAILABILITY, value=None, sample_size=0)
    assert result.met is None
    assert "no data" in result.summary


def test_read_latency_is_declared_and_not_collected(committing_db):
    """Written down, agreed, and honestly absent.

    Latency is not persisted anywhere, and this project declined to run a metrics
    backend (ADR-010). A stub returning 0.0 would report a permanent, excellent,
    entirely fictional p95 -- so it returns None and says so.
    """
    with Session(committing_db) as s:
        scope_to_ops(s)
        result = read_latency_p95(s)
    assert result.objective is READ_LATENCY
    assert result.value is None
    assert result.met is None


# --------------------------------------------------------------------------- #
# Error budget arithmetic                                                     #
# --------------------------------------------------------------------------- #


def test_the_error_budget_is_uncomfortably_small_and_says_so():
    """99% over 30 tenant-days is 0.3 permitted misses. One bad night spends it.

    Writing this down before the first breach is the entire discipline: a budget
    chosen afterwards is a number chosen to excuse the breach.
    """
    result = SLIResult(objective=AVAILABILITY, value=29 / 30, sample_size=30)
    budget = error_budget(result)
    assert budget.allowed_failures == pytest.approx(0.3)
    assert budget.observed_failures == 1
    assert budget.exhausted is True
    assert "STOP shipping" in budget.policy


def test_a_budget_with_room_left_says_ship():
    result = SLIResult(objective=AVAILABILITY, value=1.0, sample_size=300)
    budget = error_budget(result)
    assert budget.allowed_failures == pytest.approx(3.0)
    assert budget.observed_failures == 0
    assert budget.exhausted is False
    assert "ship" in budget.policy


def test_latency_has_no_error_budget_and_that_is_deliberate():
    """"p95 under 300 ms" does not decompose into a number of allowed bad events.

    Inventing one would produce a percentage that sits beside the others, looks
    like them, and means something else entirely.
    """
    assert error_budget(SLIResult(objective=READ_LATENCY, value=250.0, sample_size=1000)) is None


def test_measure_all_returns_one_result_per_objective(committing_db):
    with Session(committing_db) as s:
        scope_to_ops(s)
        results = measure_all(s, today=TODAY)
    assert {r.objective.key for r in results} == {
        AVAILABILITY.key,
        READ_LATENCY.key,
        JUDGEMENT_RATE.key,
    }


def test_a_tenant_with_two_blocks_is_counted_once(committing_db):
    """One tenant, one advisory per day, one denominator entry.

    A vineyard with two blocks has two open `grower_config` rows. Joining them
    straight into the day series counted the tenant twice -- inflating both
    halves of the fraction, and, when the rows disagreed about the timezone,
    judging one advisory against two different mornings.

    Advisories are keyed (tenant, run_date) with no location, so one deadline per
    tenant is the only granularity the data supports.
    """
    with Session(committing_db) as s:
        scope_to_ops(s)
        _config(s, "nemea", "Europe/Athens")
        s.execute(
            text(
                "INSERT INTO grower_config (tenant, location, region, timezone, crop, "
                "irrigation_method, spray_sensitivity, kc, root_depth_m, taw_mm, mad_fraction, "
                "initial_depletion_mm, effective_rain_fraction, rain_skip_mm, refill_fraction, "
                "deltat_ideal_low, deltat_ideal_high, deltat_inversion_below, deltat_marginal_upper, "
                "wind_ideal_low_ms, wind_ideal_high_ms, spray_index_cutoff, "
                "spray_index_higher_is_better, rain_fast_hours, deps_hash) "
                "VALUES ('nemea', 'block-b', 'eu', 'Europe/Athens', 'vines', 'drip', 'high', 0.7, "
                "1.0, 120, 0.4, 0, 0.8, 2, 0.9, 2, 8, 2, 10, 0.83, 4.2, 0.5, false, 2, 'h')"
            )
        )
        _advisory(s, "nemea", TODAY, datetime(2026, 7, 29, 2, 0, tzinfo=UTC))
        s.commit()
        result = availability_by_day(s, today=TODAY, window_days=0)

    assert result.sample_size == 1, "two blocks must not double-count one tenant"
    assert result.value == 1.0
