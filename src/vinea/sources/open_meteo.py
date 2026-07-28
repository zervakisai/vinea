"""Open-Meteo, mapped onto `WeatherRow`.

Two endpoints, one contract: the archive API for history (ERA5 reanalysis) and
the forecast API for the days ahead. Both return the same hourly variables, both
are mapped by the same function, and the output is `WeatherRow`s indistinguishable
from the ones the CSV loader produces. That indistinguishability is the whole
point (ADR-002): past this module, nothing knows or cares that the numbers came
from HTTP.

What Open-Meteo does and does not have, against `vinea.ingest.WeatherRow`:

  temperature_2m              -> temp_c          native
  relative_humidity_2m        -> humidity_pct    native
  et0_fao_evapotranspiration  -> et0_mm          native FAO-56 reference ET; the
                                                 exact number the water balance
                                                 wants, no reason to recompute it
  precipitation               -> precip_mm       native
  wind_speed_10m (unit=ms)    -> wind_ms         native, in m/s on request
  dew_point_2m                -> dew_point_c      native (when requested)
  vapour_pressure_deficit     -> vpd_kpa          native (when requested)
  wind_direction_10m          -> wind_dir_deg     native (when requested)
  shortwave_radiation         -> ghi_wm2          native GHI (when requested)

  delta_t_c   -> RECONSTRUCTED as temperature_2m minus wet_bulb_temperature_2m.
                 Delta-T *is* the dry-bulb/wet-bulb depression by definition, and
                 Open-Meteo publishes wet_bulb_temperature_2m, so this is an exact
                 identity, not an approximation. If either input is missing for an
                 hour, delta_t is None for that hour -- the same "missing, not
                 guessed" rule the water balance and the spray gates already follow.

  spray_index -> None, always. Open-Meteo has no equivalent, and the spray index
                 is an opaque vendor-specific number we can't reconstruct from
                 physics. Fabricating one would be exactly the silent-pass the
                 whole system refuses: a None spray-critical cell fails its gate
                 closed, and the DataQuality 5x penalty for a missing
                 spray-critical cell lands on it -- which is the right signal:
                 this source is genuinely worse for spray timing, and the
                 confidence should say so.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx

from vinea.ingest import WeatherLoadResult, WeatherRow, assemble_load_result

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# The variables we ask both endpoints for. wet_bulb_temperature_2m is here only
# to reconstruct delta_t; it never becomes a WeatherRow field of its own.
_HOURLY_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "et0_fao_evapotranspiration",
    "precipitation",
    "wind_speed_10m",
    "wet_bulb_temperature_2m",
    "dew_point_2m",
    "vapour_pressure_deficit",
    "wind_direction_10m",
    "shortwave_radiation",
)

DEFAULT_TIMEOUT = 30.0


def _at(arr: list, i: int):
    """Value at index i, or None if the array is short/absent (a missing hour)."""
    return arr[i] if i < len(arr) else None


def _rows_from_hourly(hourly: dict) -> list[WeatherRow]:
    """Map one endpoint's `hourly` block (parallel arrays) into WeatherRows.

    Open-Meteo returns column arrays -- `time[i]`, `temperature_2m[i]`, ... all
    describe hour `i`. A `None` anywhere in an array means "not observed that
    hour", and it flows straight through to a `None` field; this adapter never
    invents a value to fill a hole, because the water balance is built to skip
    holes and would be actively misled by a fabricated zero.
    """
    times = hourly.get("time", [])
    temp = hourly.get("temperature_2m", [])
    wet_bulb = hourly.get("wet_bulb_temperature_2m", [])

    rows: list[WeatherRow] = []
    for i, ts in enumerate(times):
        air_temp = _at(temp, i)
        wb = _at(wet_bulb, i)
        # Delta-T is the wet-bulb depression, by definition. Exact when both
        # inputs are present; None (not zero, not a guess) when either isn't.
        delta_t = air_temp - wb if air_temp is not None and wb is not None else None

        rows.append(
            WeatherRow(
                timestamp=datetime.fromisoformat(ts),
                temp_c=air_temp,
                humidity_pct=_at(hourly.get("relative_humidity_2m", []), i),
                wind_ms=_at(hourly.get("wind_speed_10m", []), i),
                precip_mm=_at(hourly.get("precipitation", []), i),
                # No source for this in Open-Meteo. Honest None, not a guess.
                spray_index=None,
                et0_mm=_at(hourly.get("et0_fao_evapotranspiration", []), i),
                dew_point_c=_at(hourly.get("dew_point_2m", []), i),
                vpd_kpa=_at(hourly.get("vapour_pressure_deficit", []), i),
                delta_t_c=delta_t,
                wind_dir_deg=_at(hourly.get("wind_direction_10m", []), i),
                ghi_wm2=_at(hourly.get("shortwave_radiation", []), i),
            )
        )
    return rows


def rows_from_response(payload: dict) -> list[WeatherRow]:
    """Public entry point for mapping a raw Open-Meteo JSON body to rows.

    Split out from the HTTP so the mapping is testable against a saved response
    with no network at all -- which is how the offline tests and the seam test
    exercise it.
    """
    return _rows_from_hourly(payload.get("hourly", {}))


class OpenMeteoError(RuntimeError):
    """Open-Meteo returned an error body (often with a 200 status)."""


class OpenMeteoSource:
    """A `WeatherSource` backed by Open-Meteo's archive + forecast endpoints.

    Holds an httpx client so a caller can inject a mock transport (the tests do)
    or a real one with custom timeouts/retries. `timezone=auto` makes the API
    return *local* naive timestamps for the location -- which is exactly what
    WeatherRow expects and what the spray gates need, since "06:00 is morning" is
    a statement about local time. Asking for UTC here would put the whole
    spray-window walk an offset out.
    """

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)

    def _fetch(self, url: str, params: dict) -> dict:
        response = self._client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            # Open-Meteo signals errors in a 200 body as often as a status code,
            # so this is not redundant with raise_for_status.
            raise OpenMeteoError(payload.get("reason", "unknown Open-Meteo error"))
        return payload

    def fetch_history(
        self, *, latitude: float, longitude: float, start_date: str, end_date: str
    ) -> list[WeatherRow]:
        payload = self._fetch(
            ARCHIVE_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "start_date": start_date,
                "end_date": end_date,
                "hourly": ",".join(_HOURLY_VARIABLES),
                "wind_speed_unit": "ms",
                "timezone": "auto",
            },
        )
        return rows_from_response(payload)

    def fetch_forecast(
        self, *, latitude: float, longitude: float, forecast_days: int
    ) -> list[WeatherRow]:
        payload = self._fetch(
            FORECAST_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "hourly": ",".join(_HOURLY_VARIABLES),
                "wind_speed_unit": "ms",
                "timezone": "auto",
                "forecast_days": forecast_days,
            },
        )
        return rows_from_response(payload)

    def load(
        self,
        *,
        latitude: float,
        longitude: float,
        history_days: int,
        forecast_days: int,
        run_date: date,
    ) -> WeatherLoadResult:
        """Fetch both endpoints and assemble one WeatherLoadResult.

        The last line is the seam: `assemble_load_result` is the identical
        assessment the CSV path uses, so gaps, staleness, and missing-cell
        penalties are computed the same way regardless of source. The
        Open-Meteo-specific work is all above it; below it, this is just weather.
        """
        end = date.today() - timedelta(days=1)  # archive is reliable through yesterday
        start = end - timedelta(days=history_days - 1)

        history = self.fetch_history(
            latitude=latitude, longitude=longitude, start_date=str(start), end_date=str(end)
        )
        forecast = self.fetch_forecast(
            latitude=latitude, longitude=longitude, forecast_days=forecast_days
        )
        # Say the quiet part out loud: Open-Meteo has no spray index, so every
        # hour is missing a spray-critical reading, which drives the confidence
        # penalty up. A grower reading the notes should see the cause.
        notes = (
            "source: open-meteo",
            "open-meteo provides no spray index; every hour's spray_index is missing, so the "
            "spray leg runs at capped confidence and spray windows rest on the other gates alone "
            "-- prefer a source with a spray index for spray-critical decisions",
        )
        return assemble_load_result(history, forecast, run_date, extra_notes=notes)
