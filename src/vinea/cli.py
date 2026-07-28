"""The one-command entrypoint: load both CSVs -> run pipeline -> print result.

    uv run vinea            # default: data/ CSVs, run_date = today
    uv run vinea --run-date 2026-07-28
    uv run vinea --json     # full FarmFeatures dump (incl. per-hour spray rows)
    python -m vinea --data-dir ./data

phase 2 prints the deterministic FarmFeatures; the LLM advisory lands in phase 3.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import config
from .contracts import FarmFeatures
from .ingest import load_weather
from .pipeline import run_pipeline


def _print_summary(f: FarmFeatures) -> None:
    irr, spr = f.irrigation, f.spray
    print(f"=== Vinea deterministic features for {f.target_date.isoformat()} (as of {f.as_of}) ===")
    print(f"  model (phase 3): {config.MODEL}")
    print("\n[irrigation]")
    print(f"  current depletion : {irr.current_depletion_mm} mm  (RAW trigger {irr.raw_mm} mm, TAW {irr.taw_mm} mm)")
    print(f"  cumulative ETc    : {irr.cumulative_etc_mm} mm   (Kc={irr.kc})")
    print(f"  tomorrow ETc/rain : {irr.etc_tomorrow_mm} mm / {irr.forecast_rain_tomorrow_mm} mm")
    print(f"  -> should irrigate (mechanical trigger): {irr.should_irrigate_trigger}"
          f"  depth {irr.recommended_depth_mm} mm")
    print("\n[spray]")
    print(f"  bands tomorrow    : {spr.band_counts}")
    if spr.windows:
        for w in spr.windows:
            print(f"  window  {w.start:%H:%M}-{w.end:%H:%M}  — {w.reason}")
    else:
        print("  no sprayable window")
    if spr.limiting_factors:
        print(f"  limiting factors  : {'; '.join(spr.limiting_factors)}")
    if f.data_quality.notes:
        print("\n[data-quality] " + "; ".join(f.data_quality.notes))


def _find_csv(data_dir: Path, marker: str) -> Path:
    """Resolve a CSV by filename marker (names carry a generation timestamp)."""
    matches = sorted(data_dir.glob(f"*{marker}*.csv"))
    if not matches:
        raise FileNotFoundError(f"No CSV matching *{marker}*.csv in {data_dir}")
    return matches[-1]  # newest by lexical (timestamped) name


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="vinea",
        description="Vineyard daily advisory — should I irrigate? can I spray? (phase 1 scaffold)",
    )
    p.add_argument("--data-dir", default=None, help="dir holding the two CSVs (default: ./data)")
    p.add_argument("--history", default=None, help="override path to the last-30d CSV")
    p.add_argument("--forecast", default=None, help="override path to the next-7d CSV")
    p.add_argument("--run-date", default=None, help="YYYY-MM-DD; 'today' the advisory is for (default: today)")
    p.add_argument("--json", action="store_true", help="dump full FarmFeatures as JSON")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else config.DEFAULT_DATA_DIR
    run_date = date.fromisoformat(args.run_date) if args.run_date else date.today()

    # Missing CSVs are operator error (not dirty data) — fail with a clear message, not a traceback.
    try:
        history = Path(args.history) if args.history else _find_csv(data_dir, "last-30d")
        forecast = Path(args.forecast) if args.forecast else _find_csv(data_dir, "next-7d")
        hist, fc, dq = load_weather(
            history, forecast, run_date, staleness_threshold_hours=config.STALENESS_THRESHOLD_HOURS
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("hint: place the two CSVs in ./data/ or pass --data-dir / --history / --forecast", file=sys.stderr)
        return 2
    features = run_pipeline(hist, fc, dq, run_date)

    if args.json:
        print(features.model_dump_json(indent=2))
    else:
        _print_summary(features)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
