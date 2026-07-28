"""phase 3 agent + graph tests — all via TestModel/override, never a live model."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic_ai import capture_run_messages
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.test import TestModel

from vinea.agents import (
    IrrDeps,
    coordinator_agent,
    irrigation_agent,
    render_irrigation_context,
    run_coordinator_agent,
    run_irrigation_agent,
    run_spray_agent,
    spray_agent,
)
from vinea.contracts import DailyFarmAdvisory, IrrigationAdvice, SprayAdvice
from vinea.deps import WINE_GRAPES, Deps
from vinea.features import build_features
from vinea.graph import mermaid, run_advisory_sync
from vinea.ingest import DataQuality, WeatherRow, load_weather

DATA = Path(__file__).resolve().parents[1] / "data"
RUN_DATE = date(2026, 7, 28)
TOMORROW = date(2026, 7, 29)


def _data_csv(marker: str) -> Path:
    return sorted(DATA.glob(f"*{marker}*.csv"))[-1]


def _loaded():
    return load_weather(_data_csv("last-30d"), _data_csv("next-7d"), RUN_DATE)


def _features():
    hist, fc, dq = _loaded()
    return build_features(hist, fc, dq, RUN_DATE, WINE_GRAPES)


def _irr_args(depletion: float, *, irrigate: bool = True) -> dict:
    return {
        "target_date": TOMORROW.isoformat(),
        "should_irrigate_tomorrow": irrigate,
        "recommended_depth_mm": 150.0 if irrigate else None,
        "current_depletion_mm": depletion,
        "confidence": 0.9,
        "rationale": "test rationale",
        "evidence": [],
    }


def _spray_args() -> dict:
    return {
        "target_date": TOMORROW.isoformat(),
        "can_spray_tomorrow": False,
        "recommended_windows": [],
        "limiting_factors": ["test: declined for unit test"],
        "confidence": 0.4,
        "rationale": "test",
        "evidence": [],
    }


# --- irrigation agent ----------------------------------------------------------

def test_irrigation_agent_returns_typed_advice():
    f = _features()
    args = _irr_args(f.irrigation.current_depletion_mm)
    with irrigation_agent.override(model=TestModel(custom_output_args=args)):
        out = asyncio.run(run_irrigation_agent(WINE_GRAPES, f.irrigation, f.data_quality, TOMORROW, RUN_DATE))
    assert isinstance(out, IrrigationAdvice)
    assert out.should_irrigate_tomorrow is True
    assert out.current_depletion_mm == f.irrigation.current_depletion_mm


def test_irrigation_validator_rejects_ungrounded_depletion():
    f = _features()
    bad = _irr_args(f.irrigation.current_depletion_mm + 99)  # invented depletion
    with irrigation_agent.override(model=TestModel(custom_output_args=bad)):
        with pytest.raises(UnexpectedModelBehavior, match="Exceeded maximum output retries"):
            asyncio.run(run_irrigation_agent(WINE_GRAPES, f.irrigation, f.data_quality, TOMORROW, RUN_DATE))


# --- spray agent ---------------------------------------------------------------

def test_spray_agent_returns_typed_advice():
    f = _features()
    with spray_agent.override(model=TestModel(custom_output_args=_spray_args())):
        out = asyncio.run(run_spray_agent(WINE_GRAPES, f.spray, f.data_quality, TOMORROW, RUN_DATE))
    assert isinstance(out, SprayAdvice)
    assert out.can_spray_tomorrow is False


def test_spray_validator_accepts_trimmed_candidate_window():
    f = _features()
    cand = f.spray.windows[0]  # a real deterministic candidate
    trimmed = {"start": cand.start.isoformat(), "end": (cand.start + timedelta(hours=1)).isoformat(),
               "reason": "trimmed to first hour", "limiting_factors": []}
    args = {"target_date": TOMORROW.isoformat(), "can_spray_tomorrow": True,
            "recommended_windows": [trimmed], "limiting_factors": [], "confidence": 0.6,
            "rationale": "x", "evidence": []}
    with spray_agent.override(model=TestModel(custom_output_args=args)):
        out = asyncio.run(run_spray_agent(WINE_GRAPES, f.spray, f.data_quality, TOMORROW, RUN_DATE))
    assert out.can_spray_tomorrow is True
    assert len(out.recommended_windows) == 1


def test_spray_validator_rejects_invented_window():
    f = _features()
    bad = {
        "target_date": TOMORROW.isoformat(), "can_spray_tomorrow": True,
        "recommended_windows": [
            {"start": "2026-07-29T12:00:00", "end": "2026-07-29T12:30:00",
             "reason": "invented", "limiting_factors": []}  # 12:00 is outside every candidate window
        ],
        "limiting_factors": [], "confidence": 0.5, "rationale": "x", "evidence": [],
    }
    with spray_agent.override(model=TestModel(custom_output_args=bad)):
        with pytest.raises(UnexpectedModelBehavior, match="Exceeded maximum output retries"):
            asyncio.run(run_spray_agent(WINE_GRAPES, f.spray, f.data_quality, TOMORROW, RUN_DATE))


# --- coordinator ---------------------------------------------------------------

def _advices():
    f = _features()
    irr = IrrigationAdvice(**_irr_args(f.irrigation.current_depletion_mm))   # confidence 0.9
    spr = SprayAdvice(**_spray_args())                                       # confidence 0.4
    return f, irr, spr


def test_coordinator_assembles_verbatim_advisory():
    f, irr, spr = _advices()
    rec = {"summary": "Irrigate after sunset; hold spraying.", "conflicts_resolved": ["independent"],
           "overall_confidence": 0.4}
    with coordinator_agent.override(model=TestModel(custom_output_args=rec)):
        adv = asyncio.run(run_coordinator_agent(WINE_GRAPES, irr, spr, ["fact"], f.data_quality, TOMORROW, RUN_DATE))
    assert isinstance(adv, DailyFarmAdvisory)
    assert adv.irrigation == irr and adv.spray == spr  # re-attached verbatim by construction
    assert adv.overall_confidence == 0.4


def test_coordinator_rejects_inflated_confidence():
    f, irr, spr = _advices()  # max leg confidence = 0.9
    rec = {"summary": "x", "conflicts_resolved": [], "overall_confidence": 0.99}
    with coordinator_agent.override(model=TestModel(custom_output_args=rec)):
        with pytest.raises(UnexpectedModelBehavior, match="Exceeded maximum output retries"):
            asyncio.run(run_coordinator_agent(WINE_GRAPES, irr, spr, ["fact"], f.data_quality, TOMORROW, RUN_DATE))


# --- full 3-agent graph end-to-end (no live model) -----------------------------

def test_full_graph_end_to_end():
    hist, fc, dq = _loaded()
    f = _features()
    irr_args = _irr_args(f.irrigation.current_depletion_mm)
    rec_args = {"summary": "Irrigate after sunset; hold spraying tomorrow.",
                "conflicts_resolved": ["plans independent: daytime spray vs after-sunset irrigation"],
                "overall_confidence": 0.8}
    with irrigation_agent.override(model=TestModel(custom_output_args=irr_args)), \
         spray_agent.override(model=TestModel(custom_output_args=_spray_args())), \
         coordinator_agent.override(model=TestModel(custom_output_args=rec_args)):
        advisory = run_advisory_sync(hist, fc, dq, RUN_DATE)
    assert isinstance(advisory, DailyFarmAdvisory)
    assert advisory.date == TOMORROW
    assert advisory.irrigation.should_irrigate_tomorrow is True
    assert advisory.spray.can_spray_tomorrow is False
    assert advisory.overall_confidence == 0.8
    assert advisory.conflicts_resolved  # coordinator reconciled, didn't just concatenate


def test_graph_handles_rainy_forecast_and_no_ghi():
    # Issue #10 robustness: a rainy, unsuitable, GHI-less day must not crash.
    history = [WeatherRow(timestamp=datetime(2026, 7, 21) + timedelta(hours=i), et0_mm=1.0, precip_mm=0.0)
               for i in range(48)]
    forecast = [WeatherRow(timestamp=datetime(2026, 7, 29, h), et0_mm=0.2, precip_mm=2.0,
                           delta_t_c=12.0, wind_ms=2.0, ghi_wm2=0.0) for h in range(24)]
    dq = DataQuality(rows_loaded=72, rows_dropped=0, gap_count=0, max_gap_hours=0, nan_cells=0,
                     spray_critical_nan_cells=0, is_stale=False, staleness_hours=0.0,
                     forecast_covers_tomorrow=True)
    feats = build_features(history, forecast, dq, RUN_DATE, WINE_GRAPES)
    irr_args = _irr_args(feats.irrigation.current_depletion_mm, irrigate=feats.irrigation.should_irrigate_trigger)
    rec_args = {"summary": "rainy day", "conflicts_resolved": ["rain limits spraying"], "overall_confidence": 0.3}
    with irrigation_agent.override(model=TestModel(custom_output_args=irr_args)), \
         spray_agent.override(model=TestModel(custom_output_args=_spray_args())), \
         coordinator_agent.override(model=TestModel(custom_output_args=rec_args)):
        adv = run_advisory_sync(history, forecast, dq, RUN_DATE)
    assert isinstance(adv, DailyFarmAdvisory)
    assert adv.spray.can_spray_tomorrow is False


# --- dynamic system prompt (#11) ----------------------------------------------

def test_dynamic_prompt_reflects_deps():
    f = _features()
    d1 = IrrDeps(crop=WINE_GRAPES, features=f.irrigation, data_quality=f.data_quality,
                 target_date=TOMORROW, run_date=RUN_DATE)
    d2 = IrrDeps(crop=Deps(crop="table grapes", kc=0.85), features=f.irrigation,
                 data_quality=f.data_quality, target_date=TOMORROW, run_date=RUN_DATE)
    s1 = render_irrigation_context(d1)
    s2 = render_irrigation_context(d2)
    assert "wine grapes" in s1 and "Kc=0.7" in s1
    assert "2026-07-28" in s1 and "2026-07-29" in s1            # today AND target date
    assert str(f.irrigation.current_depletion_mm) in s1        # the COMPUTED number, not a constant
    assert s1 != s2 and "table grapes" in s2 and "0.85" in s2


def test_instructions_rendered_onto_request():
    f = _features()
    with capture_run_messages() as messages:
        with irrigation_agent.override(model=TestModel(custom_output_args=_irr_args(f.irrigation.current_depletion_mm))):
            asyncio.run(run_irrigation_agent(WINE_GRAPES, f.irrigation, f.data_quality, TOMORROW, RUN_DATE))
    blob = "".join(getattr(m, "instructions", "") or "" for m in messages)
    assert "wine grapes" in blob and "Kc=0.7" in blob


# --- topology diagram stays in sync with the README ---------------------------

def test_readme_mermaid_matches_topology():
    code = mermaid()
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    for edge in ("FeatureBuilderNode --> IrrigationNode",
                 "IrrigationNode --> SprayNode",
                 "SprayNode --> CoordinatorNode"):
        assert edge in code and edge in readme
