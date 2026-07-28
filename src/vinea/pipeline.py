"""Orchestration entrypoint.

phase 1: a STUB that proves the data loads and flows end-to-end (no LLM call).
TODO(phase 2): replace `run_pipeline` with the deterministic FeatureBuilder (ETc + water
          balance, Delta-T/wind spray windows) feeding a 3-agent pydantic-graph
          (Irrigation, Spray, Coordinator) via `graph.run(...)`, returning a real
          `DailyFarmAdvisory` (typed models land in #5).
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel

from .ingest import DataQuality, WeatherRow


class StubAdvisory(BaseModel):
    """Placeholder shaped like the eventual DailyFarmAdvisory (see #5)."""

    date: date
    irrigation: str
    spray: str
    summary: str
    conflicts_resolved: list[str]
    data_quality: DataQuality


def run_pipeline(
    history: list[WeatherRow],
    forecast: list[WeatherRow],
    data_quality: DataQuality,
    run_date: date,
) -> StubAdvisory:
    tomorrow = run_date + timedelta(days=1)
    return StubAdvisory(
        date=tomorrow,
        irrigation="TODO(phase 2): IrrigationAdvice from water-balance FeatureBuilder + Irrigation Agent",
        spray="TODO(phase 2): SprayAdvice from Delta-T/wind FeatureBuilder + Spray Agent",
        summary=(
            f"Scaffold OK. Loaded {data_quality.rows_loaded} validated rows "
            f"({len(history)} history + {len(forecast)} forecast); "
            f"data confidence_penalty={data_quality.confidence_penalty}. "
            "Agronomy + agents land in phase 2/phase 3."
        ),
        conflicts_resolved=[],
        data_quality=data_quality,
    )
