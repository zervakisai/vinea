"""The CSV loader, wrapped as a `WeatherSource`.

the core's `ingest.load_weather` already reads the two fixture CSVs and returns
(history, forecast, quality). This is the thin adapter that presents it as a
`WeatherSource`, so the CLI and the seam test can treat "the file on disk" and
"the Open-Meteo feed" through the identical Protocol. The CSV path stays -- it's
what the offline tests and reproducible runs use -- it just gains a uniform face.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from vinea.ingest import WeatherLoadResult, load_weather


class CsvSource:
    """A `WeatherSource` backed by the two CSV files.

    Ignores latitude/longitude/history_days/forecast_days -- the file is the
    file. Constructed with the two paths; `load` just runs the the core loader and
    boxes its tuple into the seam's `WeatherLoadResult`.
    """

    def __init__(
        self,
        history_path: str | Path,
        forecast_path: str | Path,
        *,
        staleness_threshold_hours: int = 48,
    ) -> None:
        self.history_path = history_path
        self.forecast_path = forecast_path
        self.staleness_threshold_hours = staleness_threshold_hours

    def load(
        self,
        *,
        latitude: float = 0.0,
        longitude: float = 0.0,
        history_days: int = 30,
        forecast_days: int = 7,
        run_date: date,
    ) -> WeatherLoadResult:
        history, forecast, quality = load_weather(
            self.history_path,
            self.forecast_path,
            run_date,
            staleness_threshold_hours=self.staleness_threshold_hours,
        )
        return WeatherLoadResult(history=history, forecast=forecast, quality=quality)
