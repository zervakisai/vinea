"""Ingestion + graceful-degradation tests. Pure Python — no live model."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from vinea.ingest import load_weather
from vinea.pipeline import run_pipeline

DATA = Path(__file__).resolve().parents[1] / "data"
FIX = Path(__file__).resolve().parent / "fixtures"
RUN_DATE = date(2026, 7, 28)


def _data_csv(marker: str) -> Path:
    return sorted(DATA.glob(f"*{marker}*.csv"))[-1]


# --- Happy path: the real (clean) shipped CSVs -------------------------------

def test_real_csv_row_counts():
    hist, fc, dq = load_weather(_data_csv("last-30d"), _data_csv("next-7d"), RUN_DATE)
    assert len(hist) == 720
    assert len(fc) == 168
    assert dq.rows_dropped == 0
    assert dq.nan_cells == 0
    assert dq.gap_count == 0


def test_real_csv_clean_field_mapping():
    hist, _, _ = load_weather(_data_csv("last-30d"), _data_csv("next-7d"), RUN_DATE)
    first = hist[0]
    # Unicode headers mapped to clean fields and coerced to float.
    assert first.temp_c == pytest.approx(25.70)
    assert first.et0_mm == pytest.approx(0.09)
    assert first.delta_t_c == pytest.approx(8.40)


def test_real_rows_returned_sorted():
    hist, fc, _ = load_weather(_data_csv("last-30d"), _data_csv("next-7d"), RUN_DATE)
    assert hist == sorted(hist, key=lambda r: r.timestamp)
    assert fc == sorted(fc, key=lambda r: r.timestamp)


def test_real_data_is_fresh_and_covers_tomorrow():
    # The committed capture ends at yesterday 23:00 and forecasts 7 days forward, so
    # it must NOT be flagged. Staleness is covered by fixture_stale.csv instead.
    _, _, dq = load_weather(_data_csv("last-30d"), _data_csv("next-7d"), RUN_DATE)
    assert dq.is_stale is False
    assert dq.staleness_hours <= 48
    assert dq.forecast_covers_tomorrow is True   # forecast spans tomorrow
    assert dq.confidence_penalty == 0.0          # clean, fresh, gap-free: nothing to penalise


# --- Degradation: synthetic corrupted fixtures -------------------------------

def test_empty_cells_coerced_to_none_and_penalized():
    # corrupted file as history; a clean file as forecast (so signals aren't double-counted).
    hist, _, dq = load_weather(FIX / "fixture_nan.csv", FIX / "fixture_clean.csv", RUN_DATE)
    assert dq.rows_dropped == 0              # empty cells degrade, they don't drop the row
    assert dq.nan_cells == 3                 # et0(1) + wind(1) + delta_t(1)
    assert dq.spray_critical_nan_cells == 2  # wind(1) + delta_t(1)
    assert any(r.delta_t_c is None for r in hist)
    assert dq.confidence_penalty > 0         # missing cells now actually lower confidence


def test_inf_treated_as_missing():
    # 'inf' must not survive as a float that poisons sums; it becomes None like NaN.
    import csv, tempfile

    p = Path(tempfile.mkdtemp()) / "inf.csv"
    p.write_text(
        "Timestamp,Wind Speed (m/s),Delta T (°C)\n"
        "2026-07-27T08:00:00,inf,6.0\n"
        "2026-07-27T09:00:00,2.0,-inf\n",
        encoding="utf-8",
    )
    hist, _, dq = load_weather(p, FIX / "fixture_clean.csv", RUN_DATE)
    assert all(r.wind_ms != float("inf") for r in hist)
    assert dq.nan_cells == 2  # the inf wind + the -inf delta_t


def test_gap_detected_and_noted():
    _, _, dq = load_weather(FIX / "fixture_gap.csv", FIX / "fixture_clean.csv", RUN_DATE)
    assert dq.gap_count == 1                 # the missing 10:00 hour
    assert dq.max_gap_hours == 1
    assert any("missing hour" in n for n in dq.notes)


def test_unparseable_row_dropped_not_crash():
    _, _, dq = load_weather(FIX / "fixture_baddate.csv", FIX / "fixture_clean.csv", RUN_DATE)
    assert dq.rows_dropped == 1
    assert any("dropped row" in n for n in dq.notes)


def test_stale_series_flagged_isolated():
    # clean (fresh, covers-tomorrow) forecast -> only the backdated-history path flips is_stale.
    _, _, dq = load_weather(FIX / "fixture_stale.csv", FIX / "fixture_clean.csv", RUN_DATE)
    assert dq.is_stale is True
    assert dq.staleness_hours > 1000         # ~57 days behind
    assert dq.forecast_covers_tomorrow is True


# --- End-to-end: a bad-input case flows through run_pipeline without crashing -

def test_bad_input_flows_end_to_end():
    hist, fc, dq = load_weather(FIX / "fixture_nan.csv", FIX / "fixture_clean.csv", RUN_DATE)
    advisory = run_pipeline(hist, fc, dq, RUN_DATE)
    assert advisory.data_quality.nan_cells > 0
    assert advisory.date == date(2026, 7, 29)
