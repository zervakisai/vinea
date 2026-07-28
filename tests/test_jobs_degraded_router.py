"""phase 8 (S3.5 / S3.6) -- the deterministic advisory builder and the router.

Pure-Python, offline. These two are the reason "no model" and "clear-cut day"
degrade to a correct answer rather than to failure, and neither touches a database
or a model.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from vinea.contracts import (
    DailyFarmAdvisory,
    FarmFeatures,
    IrrigationFeatures,
    SprayFeatures,
    SprayWindow,
)
from vinea.deps import WINE_GRAPES
from vinea.ingest import DataQuality
from vinea.jobs.degraded import build_degraded_advisory
from vinea.jobs.router import BORDERLINE_FRACTION_OF_RAW, Route, route_for

RAW = 67.5
TARGET = date(2025, 3, 2)


def _dq(**over) -> DataQuality:
    base = dict(
        rows_loaded=100,
        rows_dropped=0,
        gap_count=0,
        max_gap_hours=0,
        nan_cells=0,
        spray_critical_nan_cells=0,
        is_stale=False,
        staleness_hours=0.0,
        forecast_covers_tomorrow=True,
    )
    base.update(over)
    return DataQuality(**base)


def _features(
    *,
    depletion: float,
    windows: tuple = (),
    quality: DataQuality | None = None,
    target: date = TARGET,
) -> FarmFeatures:
    triggered = depletion >= RAW
    irr = IrrigationFeatures(
        crop="wine grapes",
        kc=0.7,
        taw_mm=150.0,
        raw_mm=RAW,
        mad_fraction=0.45,
        initial_depletion_mm=0.0,
        effective_rain_fraction=0.8,
        rain_skip_mm=5.0,
        refill_fraction=1.0,
        cumulative_etc_mm=depletion,
        current_depletion_mm=depletion,
        etc_tomorrow_mm=5.0,
        forecast_rain_tomorrow_mm=0.0,
        effective_rain_tomorrow_mm=0.0,
        projected_depletion_mm=depletion,
        should_irrigate_trigger=triggered,
        recommended_depth_mm=round(depletion, 1) if triggered else None,
        notes=[],
    )
    spray = SprayFeatures(
        target_date=target,
        can_spray=bool(windows),
        windows=list(windows),
        limiting_factors=[] if windows else ["no hour cleared all gates"],
    )
    return FarmFeatures(
        as_of=target,
        target_date=target,
        data_quality=quality or _dq(),
        irrigation=irr,
        spray=spray,
    )


# --- S3.5: the degraded advisory --------------------------------------------


def test_degraded_advisory_is_a_valid_contract_with_every_number_from_features():
    features = _features(depletion=120.0)
    advisory = build_degraded_advisory(features, [], WINE_GRAPES)

    assert isinstance(advisory, DailyFarmAdvisory)
    # Every number is copied from features, not recomputed.
    assert advisory.irrigation.current_depletion_mm == 120.0
    assert advisory.irrigation.should_irrigate_tomorrow is True  # 120 >= 67.5
    # Refill-the-deficit identity, not a judgement.
    assert advisory.irrigation.recommended_depth_mm == pytest.approx(120.0)


def test_degraded_advisory_does_not_irrigate_below_the_trigger():
    advisory = build_degraded_advisory(_features(depletion=20.0), [], WINE_GRAPES)
    assert advisory.irrigation.should_irrigate_tomorrow is False
    assert advisory.irrigation.recommended_depth_mm is None


def test_degraded_confidence_is_only_the_data_quality_ceiling():
    # A stale feed: penalty 0.30 -> confidence 0.70, no LLM boost.
    stale = _dq(is_stale=True)
    advisory = build_degraded_advisory(_features(depletion=120.0, quality=stale), [], WINE_GRAPES)
    assert advisory.irrigation.confidence == pytest.approx(0.70)
    assert advisory.overall_confidence == pytest.approx(0.70)


def test_degraded_rationale_reads_as_deterministic_not_as_model_prose():
    advisory = build_degraded_advisory(_features(depletion=120.0), [], WINE_GRAPES)
    # It should admit what it is, not impersonate the judgement layer.
    assert "no model" in advisory.irrigation.rationale.lower()


# --- S3.6: the router -------------------------------------------------------


def test_clear_cut_dry_day_skips_the_model():
    # Depletion far below RAW, no spray windows: nothing for a model to weigh.
    decision = route_for(_features(depletion=10.0, windows=()))
    assert decision.route is Route.SKIP_MODEL


def test_depletion_near_the_trigger_routes_to_the_model():
    # Within the borderline band of RAW -> a genuine irrigate-or-not call.
    band = BORDERLINE_FRACTION_OF_RAW * RAW
    decision = route_for(_features(depletion=RAW - band / 2, windows=()))
    assert decision.route is Route.LARGE_MODEL


def test_any_spray_window_routes_to_the_model():
    # Even with clear-cut irrigation, a spray window is judgement + phrasing.
    window = SprayWindow(
        start=datetime(2025, 3, 2, 6), end=datetime(2025, 3, 2, 9), reason="wind"
    )
    decision = route_for(_features(depletion=10.0, windows=(window,)))
    assert decision.route is Route.LARGE_MODEL


def test_router_reads_only_features_and_returns_a_reason():
    # It's an if over FarmFeatures, and it explains itself for the trace.
    decision = route_for(_features(depletion=10.0))
    assert isinstance(decision.reason, str) and decision.reason


def test_the_skipped_day_and_the_degraded_day_produce_the_same_shape():
    """Router-skip and no-key both route to build_degraded_advisory, so the stored
    advisory is the same contract either way -- only the `degraded` flag and the
    provenance differ, and those live on the row, not the contract."""
    features = _features(depletion=10.0)
    assert route_for(features).route is Route.SKIP_MODEL
    advisory = build_degraded_advisory(features, [], WINE_GRAPES)
    assert isinstance(advisory, DailyFarmAdvisory)
