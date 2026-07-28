"""The seam, stated as a type.

A `WeatherSource` is anything that can produce a `WeatherLoadResult` for a
location on a given `run_date`. That is the entire contract. It says nothing
about CSVs, HTTP, authentication, retries, or units -- those are an
implementation's private business, and the fact that they don't appear here is
the point: downstream code depends on this Protocol, not on any source's
internals, so a second source is additive.

Why a `Protocol` rather than an abstract base class: a source shouldn't have to
inherit from us to qualify. The CSV loader wraps a plain function; the Open-Meteo
adapter holds an httpx client. Both satisfy "has a `load` returning a
`WeatherLoadResult`" without a shared base, and structural typing lets a checker
verify that without forcing an inheritance tree that buys nothing.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from vinea.ingest import WeatherLoadResult


@runtime_checkable
class WeatherSource(Protocol):
    """Produces a `WeatherLoadResult` (history + forecast + quality) for a location.

    One method, because one method is all the seam needs. Whatever a source must
    do to honour it -- parse a file, call two APIs, reconstruct a missing variable
    -- is behind this line and invisible past it. `run_date` is here because the
    quality verdict includes staleness, which is "how old is the freshest history
    relative to the day we're advising about".
    """

    def load(
        self,
        *,
        latitude: float,
        longitude: float,
        history_days: int,
        forecast_days: int,
        run_date: date,
    ) -> WeatherLoadResult:
        """Return history + forecast for the location, already quality-assessed.

        A CSV source ignores the geographic arguments (the file is the file); an
        API source uses them. That asymmetry is fine and expected -- the seam is
        the return type, not a demand that every source care about every argument.
        """
        ...
