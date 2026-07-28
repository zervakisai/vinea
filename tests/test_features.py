"""phase 2 deterministic-core tests: water balance, spray windows, deps injection, contracts.
Pure Python — no live model."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from vinea.contracts import (
    DailyFarmAdvisory,
    IrrigationAdvice,
    IrrigationFeatures,
    SprayAdvice,
    SprayWindow,
)
from vinea.deps import WINE_GRAPES, Deps
from vinea.features import (
    build_irrigation_features,
    classify_delta_t,
    classify_wind,
    detect_spray_windows,
    spray_features_for_tomorrow,
)
from vinea.ingest import WeatherRow, load_weather

DATA = Path(__file__).resolve().parents[1] / "data"
RUN_DATE = date(2026, 7, 28)
TOMORROW = date(2026, 7, 29)


def _data_csv(marker: str) -> Path:
    return sorted(DATA.glob(f"*{marker}*.csv"))[-1]


def mkrow(ts: datetime, *, et0=None, precip=0.0, delta_t=None, wind=None, idx=65.0) -> WeatherRow:
    return WeatherRow(
        timestamp=ts, et0_mm=et0, precip_mm=precip, delta_t_c=delta_t, wind_ms=wind, spray_index=idx
    )


# --- irrigation water balance (FAO-56) ----------------------------------------

def test_cumulative_etc_reconciles_to_grounded_number():
    hist, fc, _ = load_weather(_data_csv("last-30d"), _data_csv("next-7d"), RUN_DATE)
    feats = build_irrigation_features(hist, fc, WINE_GRAPES, RUN_DATE)
    expected = sum(r.et0_mm for r in hist) * WINE_GRAPES.kc
    assert feats.cumulative_etc_mm == pytest.approx(expected, abs=0.05)
    assert feats.cumulative_etc_mm == pytest.approx(137.98, abs=0.5)  # 197.11 mm ET0 * 0.70


def test_real_data_triggers_irrigation_and_clamps_depletion():
    hist, fc, _ = load_weather(_data_csv("last-30d"), _data_csv("next-7d"), RUN_DATE)
    feats = build_irrigation_features(hist, fc, WINE_GRAPES, RUN_DATE)
    assert 0.0 <= feats.current_depletion_mm <= WINE_GRAPES.taw_mm   # clamped
    assert feats.should_irrigate_trigger is True                     # hot, dry, deficit
    assert feats.recommended_depth_mm is not None
    assert feats.recommended_depth_mm <= WINE_GRAPES.taw_mm
    assert feats.forecast_rain_tomorrow_mm == 0.0
    assert feats.etc_tomorrow_mm > 0.0                               # tomorrow's demand populated


def test_forecast_rain_flips_trigger_off():
    # depletion well past RAW (70 mm > 67.5 mm), but heavy forecast rain tomorrow -> skip.
    hist = [mkrow(datetime(2026, 6, 1) + timedelta(hours=i), et0=1.0) for i in range(100)]
    dry = [mkrow(datetime(2026, 7, 29, 6), et0=0.5, precip=0.0)]
    wet = [mkrow(datetime(2026, 7, 29, 6), et0=0.5, precip=10.0)]   # 10 mm * 0.8 = 8 mm >= rain_skip 5

    assert build_irrigation_features(hist, dry, WINE_GRAPES, RUN_DATE).should_irrigate_trigger is True
    assert build_irrigation_features(hist, wet, WINE_GRAPES, RUN_DATE).should_irrigate_trigger is False


def test_missing_et0_skipped_not_zero_filled():
    hist = [mkrow(datetime(2026, 6, 1, 0), et0=2.0), mkrow(datetime(2026, 6, 1, 1), et0=None)]
    feats = build_irrigation_features(hist, [], WINE_GRAPES, RUN_DATE)
    assert feats.skipped_et0_hours == 1
    assert feats.cumulative_etc_mm == pytest.approx(2.0 * WINE_GRAPES.kc)


# --- spray: Delta-T / wind classification & window detection -------------------

@pytest.mark.parametrize(
    "dt,band",
    [(1.5, "inversion_risk"), (2.0, "ideal"), (4.8, "ideal"), (8.0, "ideal"),
     (8.5, "marginal"), (10.0, "marginal"), (10.5, "unsuitable"), (13.46, "unsuitable")],
)
def test_delta_t_bands_at_edges(dt, band):
    assert classify_delta_t(dt, WINE_GRAPES) == band


@pytest.mark.parametrize(
    "ws,cls", [(0.5, "near_calm"), (0.83, "ok"), (2.0, "ok"), (4.2, "ok"), (6.69, "drift")]
)
def test_wind_classification(ws, cls):
    assert classify_wind(ws, WINE_GRAPES) == cls


def test_spray_windows_collapse_and_split():
    base = datetime(2026, 7, 29)
    forecast = [
        mkrow(base.replace(hour=5), delta_t=1.0, wind=2.0),    # inversion -> unsuitable
        mkrow(base.replace(hour=6), delta_t=5.0, wind=2.0),    # ideal -> suitable
        mkrow(base.replace(hour=7), delta_t=6.0, wind=2.0),    # ideal -> suitable
        mkrow(base.replace(hour=8), delta_t=11.0, wind=2.0),   # >10 -> unsuitable (splits)
        mkrow(base.replace(hour=9), delta_t=5.0, wind=6.69),   # drift -> unsuitable
        mkrow(base.replace(hour=10), delta_t=5.0, wind=2.0),   # ideal -> suitable (isolated)
    ]
    sf = spray_features_for_tomorrow(forecast, WINE_GRAPES, RUN_DATE)
    assert sf.can_spray is True
    assert len(sf.windows) == 2
    w1, w2 = sf.windows
    assert (w1.start.hour, w1.end.hour) == (6, 8)   # 06:00–08:00 (end exclusive = 07:00 +1h)
    assert (w2.start.hour, w2.end.hour) == (10, 11)
    # boundary limiting factors cite the real gates
    assert any("inversion" in lf for lf in w1.limiting_factors)
    assert any("> 10" in lf or "unsuitable" in lf for lf in w1.limiting_factors)


def test_rain_fastness_sees_across_midnight():
    # dry tomorrow evening, but rain at 00:00-01:00 the NEXT day must knock out 22:00/23:00.
    forecast = [
        mkrow(datetime(2026, 7, 29, 21), delta_t=5.0, wind=2.0, precip=0.0),
        mkrow(datetime(2026, 7, 29, 22), delta_t=5.0, wind=2.0, precip=0.0),
        mkrow(datetime(2026, 7, 29, 23), delta_t=5.0, wind=2.0, precip=0.0),
        mkrow(datetime(2026, 7, 30, 0), delta_t=5.0, wind=2.0, precip=2.0),   # next-day rain
        mkrow(datetime(2026, 7, 30, 1), delta_t=5.0, wind=2.0, precip=2.0),
    ]
    sf = spray_features_for_tomorrow(forecast, WINE_GRAPES, RUN_DATE)
    suit = {h.timestamp.hour for h in sf.hours if h.suitable}
    assert {h.timestamp.date() for h in sf.hours} == {TOMORROW}  # output is tomorrow-only
    assert 21 in suit
    assert 22 not in suit and 23 not in suit


def test_all_unsuitable_day_returns_no_window():
    base = datetime(2026, 7, 29)
    forecast = [mkrow(base.replace(hour=h), delta_t=12.0, wind=2.0) for h in range(8, 18)]
    sf = spray_features_for_tomorrow(forecast, WINE_GRAPES, RUN_DATE)
    assert sf.can_spray is False
    assert sf.windows == []
    assert sf.limiting_factors  # non-empty explanation


def test_rainy_hours_gated_by_rain_fastness():
    base = datetime(2026, 7, 29)
    # ideal Delta-T/wind, but rain at 07:00 should knock out 05:00-07:00 (rain_fast_hours=2).
    forecast = [
        mkrow(base.replace(hour=5), delta_t=5.0, wind=2.0, precip=0.0),
        mkrow(base.replace(hour=6), delta_t=5.0, wind=2.0, precip=0.0),
        mkrow(base.replace(hour=7), delta_t=5.0, wind=2.0, precip=1.0),  # rain
        mkrow(base.replace(hour=8), delta_t=5.0, wind=2.0, precip=0.0),
        mkrow(base.replace(hour=9), delta_t=5.0, wind=2.0, precip=0.0),
    ]
    sf = spray_features_for_tomorrow(forecast, WINE_GRAPES, RUN_DATE)
    suitable_hours = {h.timestamp.hour for h in sf.hours if h.suitable}
    assert 5 not in suitable_hours and 6 not in suitable_hours and 7 not in suitable_hours
    assert 8 in suitable_hours and 9 in suitable_hours


# --- dependency injection: a new crop is a config-only change -----------------

def test_kc_injection_changes_etc():
    hist = [mkrow(datetime(2026, 6, 1, 0), et0=10.0)]
    wine = build_irrigation_features(hist, [], WINE_GRAPES, RUN_DATE)            # Kc 0.70
    table = build_irrigation_features(hist, [], Deps(crop="table grapes", kc=0.85), RUN_DATE)
    assert wine.cumulative_etc_mm == pytest.approx(7.0)
    assert table.cumulative_etc_mm == pytest.approx(8.5)


def test_no_magic_numbers_use_deps():
    # custom bands flow through (proves classify_* reads deps, not literals)
    strict = Deps(deltat_ideal=(3.0, 6.0), deltat_marginal_upper=7.0)
    assert classify_delta_t(6.5, strict) == "marginal"   # 6.5 is marginal under strict, ideal under default
    assert classify_delta_t(6.5, WINE_GRAPES) == "ideal"


# --- typed contracts: validators are runtime guardrails -----------------------

def _valid_irrigation(**kw):
    base = dict(target_date=TOMORROW, should_irrigate_tomorrow=False,
                current_depletion_mm=10.0, confidence=0.8, rationale="ok")
    base.update(kw)
    return IrrigationAdvice(**base)


def test_irrigation_validators():
    _valid_irrigation()  # ok
    with pytest.raises(ValidationError):
        _valid_irrigation(confidence=1.5)
    with pytest.raises(ValidationError):
        _valid_irrigation(recommended_depth_mm=-3)
    with pytest.raises(ValidationError):
        _valid_irrigation(current_depletion_mm=-1)
    with pytest.raises(ValidationError):  # irrigate but no depth
        _valid_irrigation(should_irrigate_tomorrow=True, recommended_depth_mm=None)
    with pytest.raises(ValidationError):  # not irrigating but carries a depth
        _valid_irrigation(should_irrigate_tomorrow=False, recommended_depth_mm=30.0)
    _valid_irrigation(should_irrigate_tomorrow=True, recommended_depth_mm=20.0)  # ok


def test_spray_window_and_advice_validators():
    t0 = datetime(2026, 7, 29, 6)
    with pytest.raises(ValidationError):  # end before start
        SprayWindow(start=t0, end=t0 - timedelta(hours=1), reason="x")
    good = SprayWindow(start=t0, end=t0 + timedelta(hours=2), reason="x")

    with pytest.raises(ValidationError):  # can't spray but no reason
        SprayAdvice(target_date=TOMORROW, can_spray_tomorrow=False, confidence=0.5, rationale="r")
    with pytest.raises(ValidationError):  # can spray but no window
        SprayAdvice(target_date=TOMORROW, can_spray_tomorrow=True, confidence=0.5, rationale="r")
    with pytest.raises(ValidationError):  # cannot spray but carries a window (contradiction)
        SprayAdvice(target_date=TOMORROW, can_spray_tomorrow=False, recommended_windows=[good],
                    limiting_factors=["x"], confidence=0.5, rationale="r")
    with pytest.raises(ValidationError):  # window on the wrong day
        SprayAdvice(target_date=date(2026, 7, 1), can_spray_tomorrow=True,
                    recommended_windows=[good], confidence=0.5, rationale="r")
    SprayAdvice(target_date=TOMORROW, can_spray_tomorrow=True,
                recommended_windows=[good], confidence=0.5, rationale="r")  # ok


def test_daily_advisory_date_alignment():
    irr = _valid_irrigation()
    spr = SprayAdvice(target_date=TOMORROW, can_spray_tomorrow=False,
                      limiting_factors=["all unsuitable"], confidence=0.5, rationale="r")
    DailyFarmAdvisory(date=TOMORROW, irrigation=irr, spray=spr, summary="s", overall_confidence=0.7)  # ok
    with pytest.raises(ValidationError):
        DailyFarmAdvisory(date=date(2026, 7, 1), irrigation=irr, spray=spr, summary="s", overall_confidence=0.7)


def test_units_in_schema():
    advice = IrrigationAdvice.model_json_schema()
    assert "mm" in advice["properties"]["recommended_depth_mm"]["description"]
    feats = IrrigationFeatures.model_json_schema()
    assert "mm" in feats["properties"]["current_depletion_mm"]["description"]
    assert "mm" in feats["properties"]["taw_mm"]["description"]
