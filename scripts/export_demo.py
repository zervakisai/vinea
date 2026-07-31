#!/usr/bin/env python3
"""Compute a month of real advisories and freeze them for the static demo.

    uv run python scripts/export_demo.py            # rewrite site/data/demo.json
    uv run python scripts/export_demo.py --days 14  # a shorter window

The demo page has no backend -- GitHub Pages serves files, not Postgres and a
worker -- so this script is where the work actually happens. It fetches real
hourly weather for two real Greek wine regions, runs the **real** pipeline over a
month of run dates, and writes what it produced.

That is the difference between a demo and a mockup: every number on the page came
out of `features.py`, by the same code path the nightly batch uses. Nothing on the
page is typed by hand, and nothing is recomputed in JavaScript -- the browser
renders numbers, it does not produce them, which is the same boundary the whole
system is arranged around.

## Why one fetch per site rather than one per run date

Thirty run dates would be thirty API calls to Open-Meteo per site, asking for
almost identical windows. Instead: one call with `past_days=90`, then slice
locally per run date, using the *same* half-open convention `DbSource` uses --
history is `[run_date - 30d, run_date)`, forecast is `[run_date, run_date + 7d)`.
An hour exactly at midnight belongs to the forecast, and getting that wrong
double-counts a day of ETc.

## What it does not do

**No model.** Every advisory here is `degraded=true` -- the deterministic path,
which is a complete answer with templated prose rather than a model's judgement.
The page says so on every card, because a demo that implied an LLM wrote text a
template wrote would be lying about the one thing this project is about.

Re-run with a provider key configured and the export could carry real agent prose;
that is a deliberate next step, not an oversight.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vinea.deps import WINE_GRAPES  # noqa: E402
from vinea.features import build_features  # noqa: E402
from vinea.ingest import WeatherRow, assemble_load_result  # noqa: E402
from vinea.jobs.degraded import build_degraded_advisory  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "data" / "demo.json"

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HISTORY_DAYS = 30
FORECAST_DAYS = 7
# 90 days back gives 30 run dates each with a full 30-day history behind it.
FETCH_PAST_DAYS = 90


@dataclass(frozen=True, slots=True)
class Site:
    """One vineyard. Real coordinates, because a demo on invented ones is a mockup."""

    key: str
    name: str
    region: str
    latitude: float
    longitude: float
    blurb: str


SITES = (
    Site(
        key="nemea",
        name="Nemea",
        region="Corinthia, Peloponnese",
        latitude=37.8125,
        longitude=22.6875,
        blurb="Agiorgitiko heartland, 600 m. Genuinely water-stressed in summer, "
        "which is what makes the irrigation question non-trivial.",
    ),
    Site(
        key="naoussa",
        name="Naoussa",
        region="Imathia, Macedonia",
        latitude=40.63,
        longitude=22.07,
        blurb="Xinomavro, on the slopes of Vermio. Cooler and wetter than Nemea — "
        "the contrast is the point: same code, different answers.",
    ),
)

# Requested from Open-Meteo. Wet bulb is here only to derive Delta-T, which is the
# dry/wet-bulb depression by definition -- an identity, not an approximation.
HOURLY = (
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "precipitation",
    "et0_fao_evapotranspiration",
    "dew_point_2m",
    "pressure_msl",
    "cloud_cover",
    "shortwave_radiation",
    "vapour_pressure_deficit",
    "wet_bulb_temperature_2m",
)


def fetch(site: Site) -> dict:
    params = {
        "latitude": site.latitude,
        "longitude": site.longitude,
        "hourly": ",".join(HOURLY),
        "wind_speed_unit": "ms",
        "past_days": FETCH_PAST_DAYS,
        "forecast_days": FORECAST_DAYS,
        "timezone": "auto",
    }
    url = f"{FORECAST_URL}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=90) as response:  # noqa: S310 - fixed https host
        if response.status != 200:
            raise SystemExit(f"Open-Meteo returned HTTP {response.status} for {site.key}")
        return json.loads(response.read())


def to_rows(payload: dict) -> list[WeatherRow]:
    """The provider's parallel arrays as `WeatherRow`s. Missing stays missing.

    A None arrives as None rather than 0.0 -- the loader's whole contract, and the
    reason the confidence penalty on the page is ever non-zero.
    """
    hourly = payload["hourly"]
    times = hourly["time"]

    def at(name: str, i: int):
        series = hourly.get(name) or []
        return series[i] if i < len(series) else None

    rows: list[WeatherRow] = []
    for i, stamp in enumerate(times):
        temperature = at("temperature_2m", i)
        wet_bulb = at("wet_bulb_temperature_2m", i)
        rows.append(
            WeatherRow(
                timestamp=datetime.fromisoformat(stamp),
                temp_c=temperature,
                humidity_pct=at("relative_humidity_2m", i),
                wind_ms=at("wind_speed_10m", i),
                precip_mm=at("precipitation", i),
                et0_mm=at("et0_fao_evapotranspiration", i),
                dew_point_c=at("dew_point_2m", i),
                pressure_hpa=at("pressure_msl", i),
                cloud_cover_pct=at("cloud_cover", i),
                ghi_wm2=at("shortwave_radiation", i),
                vpd_kpa=at("vapour_pressure_deficit", i),
                delta_t_c=(
                    None if temperature is None or wet_bulb is None else temperature - wet_bulb
                ),
            )
        )
    return rows


def slice_for(rows: list[WeatherRow], run_date: date) -> tuple[list[WeatherRow], list[WeatherRow]]:
    """History and forecast around `run_date`, half-open, as `DbSource` slices them."""
    midnight = datetime.combine(run_date, time.min)
    history = [r for r in rows if midnight - timedelta(days=HISTORY_DAYS) <= r.timestamp < midnight]
    forecast = [r for r in rows if midnight <= r.timestamp < midnight + timedelta(days=FORECAST_DAYS)]
    return history, forecast


def night(site: Site, rows: list[WeatherRow], run_date: date) -> dict | None:
    """One run date, through the real pipeline. None when the data cannot support it."""
    history, forecast = slice_for(rows, run_date)
    if not history or not forecast:
        return None

    load_result = assemble_load_result(history, forecast, run_date)
    features = build_features(
        list(load_result.history), list(load_result.forecast), load_result.quality,
        run_date, WINE_GRAPES,
    )
    advisory = build_degraded_advisory(features, list(load_result.forecast), WINE_GRAPES)

    quality = load_result.quality
    return {
        "run_date": run_date.isoformat(),
        "target_date": features.target_date.isoformat(),
        "advisory": json.loads(advisory.model_dump_json()),
        # The working, so a reader can check the headline number by hand rather
        # than trusting it. This is the demo's actual argument.
        "features": {
            "cumulative_etc_mm": features.irrigation.cumulative_etc_mm,
            "current_depletion_mm": features.irrigation.current_depletion_mm,
            "etc_tomorrow_mm": features.irrigation.etc_tomorrow_mm,
            "forecast_rain_tomorrow_mm": features.irrigation.forecast_rain_tomorrow_mm,
            "effective_rain_tomorrow_mm": features.irrigation.effective_rain_tomorrow_mm,
            "projected_depletion_mm": features.irrigation.projected_depletion_mm,
            "should_irrigate_trigger": features.irrigation.should_irrigate_trigger,
            "raw_mm": features.irrigation.raw_mm,
            "taw_mm": features.irrigation.taw_mm,
            "kc": features.irrigation.kc,
            "mad_fraction": features.irrigation.mad_fraction,
            "effective_rain_fraction": features.irrigation.effective_rain_fraction,
            "band_counts": features.spray.band_counts,
            "notes": features.irrigation.notes,
        },
        "quality": {
            "rows_loaded": quality.rows_loaded,
            "gap_count": quality.gap_count,
            "max_gap_hours": quality.max_gap_hours,
            "nan_cells": quality.nan_cells,
            "spray_critical_nan_cells": quality.spray_critical_nan_cells,
            "confidence_penalty": round(quality.confidence_penalty, 4),
            "notes": list(quality.notes),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=30, help="How many run dates to export.")
    parser.add_argument("--dry-run", action="store_true", help="Compute and summarise, write nothing.")
    args = parser.parse_args(argv)

    today = date.today()
    run_dates = [today - timedelta(days=offset) for offset in range(args.days, 0, -1)]

    export: dict = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        # Stated in the payload, not just in the page, so a copy of this file
        # cannot be passed off as something it is not.
        "provenance": {
            "weather": "Open-Meteo (https://open-meteo.com/), CC BY 4.0",
            "model": None,
            "note": (
                "Every number here was computed by vinea's features.py, from the "
                "weather above, by the same code path the nightly batch runs. "
                "All advisories are degraded=true: the deterministic path, no LLM."
            ),
        },
        "deps": asdict(WINE_GRAPES),
        "sites": [],
    }

    for site in SITES:
        print(f"fetching {site.key} ({site.latitude}, {site.longitude})…", file=sys.stderr)
        rows = to_rows(fetch(site))
        nights = [n for n in (night(site, rows, d) for d in run_dates) if n is not None]
        if not nights:
            raise SystemExit(f"{site.key}: no run date had both history and forecast")
        print(
            f"  {len(rows)} hourly rows -> {len(nights)} advisories, "
            f"depletion {nights[0]['features']['current_depletion_mm']:.1f} → "
            f"{nights[-1]['features']['current_depletion_mm']:.1f} mm",
            file=sys.stderr,
        )
        export["sites"].append(
            {
                "key": site.key,
                "name": site.name,
                "region": site.region,
                "latitude": site.latitude,
                "longitude": site.longitude,
                "blurb": site.blurb,
                "nights": nights,
            }
        )

    if args.dry_run:
        print("--dry-run: nothing written", file=sys.stderr)
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(export, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
