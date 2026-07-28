"""The one-command entrypoint: load both CSVs -> run pipeline -> print advisory.

    uv run vinea            # default: data/ CSVs, run_date = today
    uv run vinea --run-date 2026-07-28
    python -m vinea --data-dir ./data
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import config
from .ingest import load_weather
from .pipeline import run_pipeline


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
    advisory = run_pipeline(hist, fc, dq, run_date)

    print(f"Vinea daily advisory for {advisory.date.isoformat()}  (model: {config.MODEL})")
    print(advisory.model_dump_json(indent=2))
    if dq.notes:
        print("\n[data-quality] " + "; ".join(dq.notes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
