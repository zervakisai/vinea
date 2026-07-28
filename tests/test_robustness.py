"""Robustness: degraded data deterministically lowers confidence + surfaces a caveat,
the graph never crashes, and missing hours are never fabricated. No live model."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

from pydantic_ai.models.test import TestModel

from vinea.agents import (
    coordinator_agent,
    irrigation_agent,
    run_irrigation_agent,
    spray_agent,
)
from vinea.contracts import DailyFarmAdvisory
from vinea.deps import WINE_GRAPES
from vinea.features import build_features
from vinea.graph import run_advisory_sync
from vinea.ingest import DataQuality, WeatherRow

RUN_DATE = date(2026, 7, 28)
TOMORROW = date(2026, 7, 29)


def _dq(*, nan=0, crit=0, dropped=0, notes=None) -> DataQuality:
    return DataQuality(
        rows_loaded=100, rows_dropped=dropped, gap_count=0, max_gap_hours=0,
        nan_cells=nan, spray_critical_nan_cells=crit, is_stale=False, staleness_hours=0.0,
        forecast_covers_tomorrow=True, notes=notes or [],
    )


def _synth(nan_block: bool = False):
    history = [
        WeatherRow(timestamp=datetime(2026, 7, 21) + timedelta(hours=i),
                   et0_mm=1.0, precip_mm=0.0, delta_t_c=5.0, wind_ms=2.0)
        for i in range(48)
    ]
    forecast = []
    for h in range(24):
        missing = nan_block and 8 <= h <= 15
        forecast.append(WeatherRow(
            timestamp=datetime(2026, 7, 29, h),
            et0_mm=None if missing else 0.3,
            delta_t_c=None if missing else 5.0,
            precip_mm=0.0, wind_ms=2.0, ghi_wm2=(500.0 if 6 <= h <= 18 else 0.0),
        ))
    return history, forecast


def _irr_args(depletion: float) -> dict:
    return {
        "target_date": TOMORROW.isoformat(), "should_irrigate_tomorrow": False,
        "recommended_depth_mm": None, "current_depletion_mm": depletion,
        "confidence": 0.9, "rationale": "test", "evidence": [],
    }


def _spray_args() -> dict:
    return {
        "target_date": TOMORROW.isoformat(), "can_spray_tomorrow": False,
        "recommended_windows": [], "limiting_factors": ["test"],
        "confidence": 0.4, "rationale": "test", "evidence": [],
    }


def test_degraded_dq_lowers_confidence_and_adds_caveat():
    history, forecast = _synth()
    feats = build_features(history, forecast, _dq(), RUN_DATE, WINE_GRAPES)
    irr = feats.irrigation
    args = _irr_args(irr.current_depletion_mm)
    clean = _dq()
    degraded = _dq(nan=8, crit=4, notes=["tomorrow ET₀/Delta-T missing 8/24h"])
    with irrigation_agent.override(model=TestModel(custom_output_args=args)):
        a_clean = asyncio.run(run_irrigation_agent(WINE_GRAPES, irr, clean, TOMORROW, RUN_DATE))
        a_deg = asyncio.run(run_irrigation_agent(WINE_GRAPES, irr, degraded, TOMORROW, RUN_DATE))
    assert a_clean.confidence == 0.9                 # clean data -> not clamped
    assert a_deg.confidence < a_clean.confidence     # degraded -> deterministically lowered
    assert "data quality" in a_deg.rationale         # caveat surfaced, not hidden


def test_nan_hours_excluded_not_fabricated():
    history, forecast = _synth(nan_block=True)
    feats = build_features(history, forecast, _dq(crit=8), RUN_DATE, WINE_GRAPES)
    nan_hours = [h for h in feats.spray.hours if 8 <= h.timestamp.hour <= 15]
    assert nan_hours and all(h.band is None and not h.suitable for h in nan_hours)  # excluded, not guessed


def _run_advisory(nan_block: bool, dq: DataQuality) -> DailyFarmAdvisory:
    history, forecast = _synth(nan_block=nan_block)
    irr = build_features(history, forecast, dq, RUN_DATE, WINE_GRAPES).irrigation
    rec_args = {"summary": "test", "conflicts_resolved": [], "overall_confidence": 0.5}
    with irrigation_agent.override(model=TestModel(custom_output_args=_irr_args(irr.current_depletion_mm))), \
         spray_agent.override(model=TestModel(custom_output_args=_spray_args())), \
         coordinator_agent.override(model=TestModel(custom_output_args=rec_args)):
        return run_advisory_sync(history, forecast, dq, RUN_DATE)


def test_bad_input_flows_end_to_end_without_crashing():
    clean = _run_advisory(False, _dq())
    degraded = _run_advisory(True, _dq(nan=8, crit=8, notes=["tomorrow ET₀/Delta-T missing 8/24h"]))
    assert isinstance(degraded, DailyFarmAdvisory)                              # no crash
    assert degraded.irrigation.confidence < clean.irrigation.confidence        # strictly lower than a clean run
    assert degraded.spray.recommended_windows == []                            # NaN hours never fabricated into a window
    assert any("data quality" in c for c in degraded.conflicts_resolved)       # caveat surfaced to the grower


def test_cli_degrades_to_features_on_retry_exhaustion(monkeypatch, capsys):
    # Force the LLM path (set a dummy key); the override keeps it offline. An ungrounded depletion
    # exhausts retries -> UnexpectedModelBehavior -> the CLI degrades to features, no traceback.
    import vinea.config as config
    from vinea import cli

    monkeypatch.setenv(cli._KEY_ENV.get(config.MODEL.split(":", 1)[0], "ANTHROPIC_API_KEY"), "dummy-not-used")
    ungrounded = _irr_args(99999.0)  # never matches the real computed depletion
    with irrigation_agent.override(model=TestModel(custom_output_args=ungrounded)):
        rc = cli.main(["--run-date", "2026-07-28"])
    out = capsys.readouterr()
    assert rc == 1                                              # degraded exit, not a crash
    assert "deterministic features" in out.out                 # fell back to the features summary
    assert "Traceback" not in out.err
