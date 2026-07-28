#!/usr/bin/env python3
"""Regenerate the committed demo dataset from Open-Meteo.

Why this script exists: the two CSVs under `data/` are committed so the repo is
self-contained and the test suite stays offline. Committed data rots into a
mystery unless the exact call that produced it is in the repo too -- so this is
that call, runnable, rather than a paragraph describing it.

    uv run python scripts/fetch_dataset.py            # rewrite data/ in place
    uv run python scripts/fetch_dataset.py --dry-run  # print the summary only

One HTTP call to the *forecast* endpoint with `past_days` + `forecast_days`
supplies both windows. Using one endpoint for both is deliberate: the archive
endpoint (ERA5 reanalysis) lags several days, so it cannot reach yesterday, and
splicing reanalysis onto forecast would put a model discontinuity in the middle
of the history -- exactly the kind of seam the water balance would integrate
straight over. One model, one continuous series.

Data: Open-Meteo (https://open-meteo.com/), CC BY 4.0. See data/ATTRIBUTION.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# Nemea, Corinthia -- the Agiorgitiko heartland, and a genuinely water-stressed
# site in July, which is what makes the irrigation question non-trivial here.
LATITUDE = 37.8167
LONGITUDE = 22.6667
SITE = "nemea"

HISTORY_DAYS = 30
FORECAST_DAYS = 7

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Ordered: this tuple is both the request and the CSV column order.
# (open-meteo variable, CSV header) -- headers match `WeatherRow`'s aliases
# exactly, including the U+2080 subscript zero in ET₀ and U+00B0 degree signs.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("temperature_2m", "Temperature (°C)"),
    ("relative_humidity_2m", "Humidity (%)"),
    ("wind_speed_10m", "Wind Speed (m/s)"),
    ("precipitation", "Precipitation (mm/h)"),
    ("et0_fao_evapotranspiration", "Reference ET₀ (mm)"),
    ("dew_point_2m", "Dew Point (°C)"),
    ("pressure_msl", "Pressure (hPa)"),
    ("cloud_cover", "Cloud Cover (%)"),
    ("snowfall", "Snowfall (mm/h)"),
    ("wind_direction_10m", "Wind Direction (°)"),
    ("shortwave_radiation", "Solar Irradiance (GHI) (W/m2)"),
    ("vapour_pressure_deficit", "VPD (kPa)"),
)

# Delta-T is not published directly. It *is* the dry-bulb/wet-bulb depression by
# definition, and Open-Meteo publishes wet_bulb_temperature_2m, so this is an
# exact identity rather than an approximation.
DERIVED_DELTA_T = "Delta T (°C)"

# Requested only to derive Delta-T; never written as its own column.
_WET_BULB = "wet_bulb_temperature_2m"

# Open-Meteo reports snowfall in cm; the schema (and every other precipitation
# figure here) is mm. Silent unit mismatches are the classic way a dataset lies.
_UNIT_SCALE = {"snowfall": 10.0}

HEADER = ("Timestamp", *(h for _, h in COLUMNS), DERIVED_DELTA_T)


def fetch(*, past_days: int) -> dict:
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": ",".join([v for v, _ in COLUMNS] + [_WET_BULB]),
        "wind_speed_unit": "ms",
        "past_days": past_days,
        "forecast_days": FORECAST_DAYS,
        "timezone": "auto",
    }
    url = f"{FORECAST_URL}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https host
        if response.status != 200:
            raise SystemExit(f"Open-Meteo returned HTTP {response.status}")
        return json.loads(response.read())


def _rows(payload: dict, start: datetime, end: datetime) -> list[list[str]]:
    """Rows with timestamp in [start, end], formatted to the CSV schema.

    A None in any requested variable is written as an empty cell rather than a
    zero: the loader's whole contract is that missing stays missing.
    """
    hourly = payload["hourly"]
    times = hourly["time"]
    out: list[list[str]] = []

    for i, stamp in enumerate(times):
        moment = datetime.fromisoformat(stamp)
        if not (start <= moment <= end):
            continue

        cells: list[str] = [moment.isoformat(timespec="seconds")]
        for variable, _ in COLUMNS:
            value = hourly[variable][i]
            if value is None:
                cells.append("")
                continue
            cells.append(f"{value * _UNIT_SCALE.get(variable, 1.0):.2f}")

        temp = hourly["temperature_2m"][i]
        wet_bulb = hourly[_WET_BULB][i]
        cells.append("" if temp is None or wet_bulb is None else f"{temp - wet_bulb:.2f}")

        out.append(cells)

    return out


def _write(path: Path, rows: list[list[str]]) -> None:
    lines = [",".join(HEADER), *(",".join(r) for r in rows)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="fetch and report; write nothing")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data",
        help="where the two CSVs are written (default: ./data)",
    )
    args = parser.parse_args()

    # Anchor on whole local days so the two files tile exactly and the row counts
    # are the obvious 30*24 and 7*24 -- a reader should not have to count.
    today = date.today()
    history_start = datetime.combine(today - timedelta(days=HISTORY_DAYS), datetime.min.time())
    history_end = datetime.combine(today - timedelta(days=1), datetime.min.time()).replace(hour=23)
    forecast_start = datetime.combine(today, datetime.min.time())
    forecast_end = datetime.combine(
        today + timedelta(days=FORECAST_DAYS - 1), datetime.min.time()
    ).replace(hour=23)

    # +2 days of slack: past_days is counted by the API in UTC days, and the local
    # timezone shift can otherwise clip the first requested hour.
    payload = fetch(past_days=HISTORY_DAYS + 2)

    history = _rows(payload, history_start, history_end)
    forecast = _rows(payload, forecast_start, forecast_end)

    expected_history = HISTORY_DAYS * 24
    expected_forecast = FORECAST_DAYS * 24

    print(f"site       : {SITE} ({payload['latitude']}, {payload['longitude']})")
    print(f"elevation  : {payload['elevation']} m")
    print(f"timezone   : {payload['timezone']}")
    print(f"history    : {len(history)} rows (expected {expected_history})")
    print(f"forecast   : {len(forecast)} rows (expected {expected_forecast})")

    if len(history) != expected_history or len(forecast) != expected_forecast:
        print(
            "\nrefusing to write: row counts are off, which means the requested "
            "window was not fully covered. Widen past_days and retry.",
            file=sys.stderr,
        )
        return 1

    stamp = today.isoformat()
    history_path = args.data_dir / f"{SITE}_weather_last-30d_1h_{stamp}.csv"
    forecast_path = args.data_dir / f"{SITE}_weather_next-7d_1h_{stamp}.csv"

    if args.dry_run:
        print(f"\n[dry-run] would write {history_path.name} and {forecast_path.name}")
        return 0

    args.data_dir.mkdir(parents=True, exist_ok=True)
    # The loader globs *last-30d*.csv / *next-7d*.csv and takes the newest match,
    # so stale captures would silently shadow this one. Remove them.
    for old in (*args.data_dir.glob("*last-30d*.csv"), *args.data_dir.glob("*next-7d*.csv")):
        old.unlink()

    _write(history_path, history)
    _write(forecast_path, forecast)
    print(f"\nwrote {history_path}\nwrote {forecast_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
