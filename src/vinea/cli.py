"""The one-command entrypoint: load both CSVs -> run the 3-agent graph -> print advisory.

    uv run vinea                  # full LLM advisory (needs one provider API key)
    uv run vinea --features-only  # deterministic FarmFeatures only (no LLM, no key)
    uv run vinea --json           # dump JSON instead of the human summary
    uv run vinea --run-date 2026-07-28
    python -m vinea --data-dir ./data

With no API key set, falls back to --features-only automatically (with a note).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from . import config
from .contracts import DailyFarmAdvisory, FarmFeatures
from .graph import run_advisory_sync
from .ingest import load_weather
from .pipeline import run_pipeline

_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
    "google-gla": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "mistral": "MISTRAL_API_KEY",
}


def _has_api_key(model: str) -> bool:
    env = _KEY_ENV.get(model.split(":", 1)[0])
    return env is None or bool(os.getenv(env))  # unknown provider -> attempt anyway


def _find_csv(data_dir: Path, marker: str) -> Path:
    matches = sorted(data_dir.glob(f"*{marker}*.csv"))
    if not matches:
        raise FileNotFoundError(f"No CSV matching *{marker}*.csv in {data_dir}")
    return matches[-1]


def _print_summary(f: FarmFeatures) -> None:
    irr, spr = f.irrigation, f.spray
    print(f"=== Vinea deterministic features for {f.target_date.isoformat()} (as of {f.as_of}) ===")
    print("\n[irrigation]")
    print(f"  current depletion : {irr.current_depletion_mm} mm  (RAW trigger {irr.raw_mm} mm, TAW {irr.taw_mm} mm)")
    print(f"  cumulative ETc    : {irr.cumulative_etc_mm} mm   (Kc={irr.kc})")
    print(f"  tomorrow ETc/rain : {irr.etc_tomorrow_mm} mm / {irr.forecast_rain_tomorrow_mm} mm")
    print(f"  -> should irrigate (mechanical trigger): {irr.should_irrigate_trigger}  depth {irr.recommended_depth_mm} mm")
    print("\n[spray]")
    print(f"  bands tomorrow    : {spr.band_counts}")
    for w in spr.windows:
        print(f"  window  {w.start:%H:%M}-{w.end:%H:%M}  — {w.reason}")
    if not spr.windows:
        print("  no sprayable window")
    if spr.limiting_factors:
        print(f"  limiting factors  : {'; '.join(spr.limiting_factors)}")
    if f.data_quality.notes:
        print("\n[data-quality] " + "; ".join(f.data_quality.notes))


def _print_advisory(a: DailyFarmAdvisory) -> None:
    irr, spr = a.irrigation, a.spray
    print(f"=== Vinea advisory for {a.date.isoformat()}  (overall confidence {a.overall_confidence}) ===")
    print("\n[irrigation]")
    print(f"  should irrigate: {irr.should_irrigate_tomorrow}  depth {irr.recommended_depth_mm} mm "
          f"(depletion {irr.current_depletion_mm} mm, confidence {irr.confidence})")
    print(f"  {irr.rationale}")
    print("\n[spray]")
    print(f"  can spray: {spr.can_spray_tomorrow}  (confidence {spr.confidence})")
    for w in spr.recommended_windows:
        print(f"  window {w.start:%H:%M}-{w.end:%H:%M} — {w.reason}")
    if spr.limiting_factors:
        print(f"  limiting factors: {'; '.join(spr.limiting_factors)}")
    print(f"  {spr.rationale}")
    print("\n[summary]")
    print(f"  {a.summary}")
    if a.conflicts_resolved:
        print("  conflicts resolved:")
        for c in a.conflicts_resolved:
            print(f"   - {c}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="vinea",
        description="Vineyard daily advisory — should I irrigate? can I spray?",
    )
    p.add_argument("--data-dir", default=None, help="dir holding the two CSVs (default: ./data)")
    p.add_argument("--history", default=None, help="override path to the last-30d CSV")
    p.add_argument("--forecast", default=None, help="override path to the next-7d CSV")
    p.add_argument("--run-date", default=None, help="YYYY-MM-DD; 'today' the advisory is for (default: today)")
    p.add_argument("--features-only", action="store_true", help="deterministic features only (no LLM)")
    p.add_argument("--json", action="store_true", help="dump JSON instead of the human summary")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else config.DEFAULT_DATA_DIR
    run_date = date.fromisoformat(args.run_date) if args.run_date else date.today()

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

    # No key (or explicit flag) -> deterministic features only.
    if args.features_only or not _has_api_key(config.MODEL):
        if not args.features_only:
            print(f"note: no API key for '{config.MODEL}' — showing deterministic features only "
                  f"(set the provider key or use --features-only to silence).", file=sys.stderr)
        feats = run_pipeline(hist, fc, dq, run_date)
        print(feats.model_dump_json(indent=2)) if args.json else _print_summary(feats)
        return 0

    try:
        advisory = run_advisory_sync(hist, fc, dq, run_date)
    except Exception as exc:  # auth/network/model error -> graceful fallback
        print(f"error running advisory graph: {exc}", file=sys.stderr)
        print("falling back to deterministic features (--features-only):", file=sys.stderr)
        _print_summary(run_pipeline(hist, fc, dq, run_date))
        return 1

    print(advisory.model_dump_json(indent=2)) if args.json else _print_advisory(advisory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
