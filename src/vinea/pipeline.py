"""Orchestration entrypoint.

Load -> validate -> deterministic FeatureBuilder -> FarmFeatures. No model involved.
TODO: wire the 3-agent pydantic-graph (Irrigation, Spray, Coordinator) over these
          features via `graph.run(...)`, returning a `DailyFarmAdvisory`. The FeatureBuilder
          becomes a non-agent `BaseNode`; FarmFeatures lands on `GraphRunContext.state`.
"""

from __future__ import annotations

from datetime import date

from .contracts import FarmFeatures
from .deps import WINE_GRAPES, Deps
from .features import build_features
from .ingest import DataQuality, WeatherRow


def run_pipeline(
    history: list[WeatherRow],
    forecast: list[WeatherRow],
    data_quality: DataQuality,
    run_date: date,
    deps: Deps = WINE_GRAPES,
) -> FarmFeatures:
    return build_features(history, forecast, data_quality, run_date, deps=deps)
