"""Tests for the deterministic conflict facts the Coordinator reconciles over."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from vinea.contracts import IrrigationAdvice, SprayAdvice, SprayWindow
from vinea.deps import WINE_GRAPES
from vinea.ingest import WeatherRow, load_weather
from vinea.reconcile import build_conflict_facts, daylight_bounds

DATA = Path(__file__).resolve().parents[1] / "data"
RUN_DATE = date(2026, 7, 28)
TOMORROW = date(2026, 7, 29)


def _data_csv(marker: str) -> Path:
    return sorted(DATA.glob(f"*{marker}*.csv"))[-1]


def _irr() -> IrrigationAdvice:
    return IrrigationAdvice(target_date=TOMORROW, should_irrigate_tomorrow=False,
                            current_depletion_mm=50.0, confidence=0.8, rationale="r")


def _spray_with_window() -> SprayAdvice:
    win = SprayWindow(start=datetime(2026, 7, 29, 6), end=datetime(2026, 7, 29, 9), reason="r")
    return SprayAdvice(target_date=TOMORROW, can_spray_tomorrow=True,
                       recommended_windows=[win], confidence=0.6, rationale="r")


def test_daylight_bounds_from_ghi():
    _, fc, _ = load_weather(_data_csv("last-30d"), _data_csv("next-7d"), RUN_DATE)
    bounds = daylight_bounds(fc, TOMORROW)
    assert bounds is not None
    sunrise, sunset = bounds
    assert sunrise < sunset


def test_rain_fastness_is_bounded_to_horizon():
    spray = _spray_with_window()  # window ends 09:00; rain_fast_hours=2 -> horizon to 11:00
    far = [WeatherRow(timestamp=datetime(2026, 7, 29, 12), precip_mm=2.0, ghi_wm2=500.0)]  # beyond horizon
    near = [WeatherRow(timestamp=datetime(2026, 7, 29, 10), precip_mm=2.0, ghi_wm2=500.0)]  # within horizon
    assert any("no rain-fastness conflict" in f
               for f in build_conflict_facts(far, _irr(), spray, WINE_GRAPES, TOMORROW))
    assert any("rain-fastness risk" in f
               for f in build_conflict_facts(near, _irr(), spray, WINE_GRAPES, TOMORROW))


def test_drip_reports_no_canopy_conflict():
    facts = build_conflict_facts([], _irr(), _spray_with_window(), WINE_GRAPES, TOMORROW)
    assert any("drip" in f and "canopy" in f for f in facts)


def test_no_ghi_is_handled_gracefully():
    facts = build_conflict_facts([], _irr(), _spray_with_window(), WINE_GRAPES, TOMORROW)
    assert any("daylight bounds unavailable" in f for f in facts)  # no crash, explicit note
