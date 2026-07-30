"""CSV ingestion -> validated, clean-named rows + a DataQuality degradation signal.

This module owns robustness for DATA. A swallowed NaN/inf poisons the
running water balance (`nan + x == nan`), so "missing/non-finite" is made an *explicit
typed state* (`None`) that a policy layer routes, rather than a silent crash or poison.

Timezone policy: CSV timestamps are ISO-8601 with **no offset** (e.g.
`2026-06-28T00:00:00`), so they parse as **tz-naive** datetimes. We treat them — and the
`run_date` used for staleness — as the **same site-local wall-clock zone** (Peloponnese).
Mixing a UTC series with a local `run_date` would skew `staleness_hours` by the offset.
TODO: if a zone ever becomes known, normalize both sides to UTC.

Pure standard-library Python (csv + datetime) — no pandas, no LLM, no agent code.
(pandas `reindex(...).asfreq('h')` would be the convenience equivalent of gap detection.)
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    field_validator,
)

# Strings that mean "no value" in a cell -> coerced to None (the policy layer decides).
_MISSING_TOKENS = {"", "nan", "null", "none", "na", "n/a", "-"}
# Non-finite string tokens are treated as missing too (they poison sums exactly like NaN).
_NONFINITE_TOKENS = {"inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}

# Numeric fields we model (everything except the required timestamp).
_NUMERIC_FIELDS = (
    "temp_c",
    "humidity_pct",
    "wind_ms",
    "precip_mm",
    "spray_index",
    "et0_mm",
    "dew_point_c",
    "vpd_kpa",
    "delta_t_c",
    "wind_dir_deg",
    "ghi_wm2",
)

# Fields where a missing value means the hour is physically NOT sprayable (never guessed).
# Counted separately and penalised harder; the window exclusion itself is in features.py.
SPRAY_CRITICAL_FIELDS = ("delta_t_c", "wind_ms")


class WeatherRow(BaseModel):
    """One validated hourly observation/forecast with clean snake_case fields.

    Unicode headers are mapped via `alias` (the single source of truth for the rename):
    `Reference ET₀ (mm)` uses U+2080 subscript-zero, `°C`/`°` use U+00B0.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # tz-naive, assumed site-local (see module docstring).
    timestamp: datetime = Field(alias="Timestamp")
    temp_c: float | None = Field(default=None, alias="Temperature (°C)")
    humidity_pct: float | None = Field(default=None, alias="Humidity (%)")
    wind_ms: float | None = Field(default=None, alias="Wind Speed (m/s)")
    precip_mm: float | None = Field(default=None, alias="Precipitation (mm/h)")
    spray_index: float | None = Field(default=None, alias="Spray Index")
    et0_mm: float | None = Field(default=None, alias="Reference ET₀ (mm)")
    dew_point_c: float | None = Field(default=None, alias="Dew Point (°C)")
    vpd_kpa: float | None = Field(default=None, alias="VPD (kPa)")
    delta_t_c: float | None = Field(default=None, alias="Delta T (°C)")
    wind_dir_deg: float | None = Field(default=None, alias="Wind Direction (°)")
    ghi_wm2: float | None = Field(default=None, alias="Solar Irradiance (GHI) (W/m2)")  # daylight bounds
    # TODO: Pressure (hPa), Cloud Cover (%), Snowfall (mm/h) are parsed-but-unused
    #            (dropped via extra='ignore') — intentionally cut, not forgotten.

    @field_validator(*_NUMERIC_FIELDS, mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        """Map empty / NaN / inf cells to None *before* float coercion, so the policy
        layer (not Pydantic) decides skip-vs-interpolate. We avoid allow_inf_nan=False
        (which raises) — we want missing/non-finite to be an explicit, routable state."""
        if v is None:
            return None
        if isinstance(v, str):
            token = v.strip().lower()
            if token in _MISSING_TOKENS or token in _NONFINITE_TOKENS:
                return None
        if isinstance(v, float) and not math.isfinite(v):  # NaN, inf, -inf
            return None
        return v


class DataQuality(BaseModel):
    """Aggregate degradation signal the agents/coordinator read to lower confidence."""

    rows_loaded: int
    rows_dropped: int
    gap_count: int                # total missing hours across the series
    max_gap_hours: int            # longest single run of missing hours
    nan_cells: int                # present-but-empty/non-finite numeric cells -> None
    spray_critical_nan_cells: int # subset of nan_cells in Delta T / wind (worse)
    is_stale: bool                # history anchor too old relative to run_date
    staleness_hours: float        # hours from the freshest history row to run_date
    forecast_covers_tomorrow: bool
    notes: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def confidence_penalty(self) -> float:
        """0..1 penalty. Agents clamp confidence to a (1 - confidence_penalty) ceiling."""
        p = 0.0
        if self.rows_dropped:
            p += min(0.2, 0.02 * self.rows_dropped)
        if self.gap_count:
            p += min(0.2, 0.01 * self.gap_count)
        if self.nan_cells:
            p += min(0.15, 0.01 * self.nan_cells)
        if self.spray_critical_nan_cells:
            p += min(0.2, 0.05 * self.spray_critical_nan_cells)
        if self.is_stale:
            p += 0.3
        if not self.forecast_covers_tomorrow:
            p += 0.3
        return round(min(p, 0.9), 3)


def _detect_gaps(timestamps: list[datetime]) -> tuple[int, int]:
    """Count missing hours by diffing consecutive timestamps (expect a 1h step)."""
    if len(timestamps) < 2:
        return 0, 0
    ts = sorted(timestamps)
    gap_count = 0
    max_gap = 0
    for a, b in zip(ts, ts[1:]):
        missing = round((b - a).total_seconds() / 3600) - 1
        if missing > 0:
            gap_count += missing
            max_gap = max(max_gap, missing)
    return gap_count, max_gap


def _present(fields: tuple[str, ...], fieldnames: list[str]) -> list[str]:
    """Subset of `fields` whose source column actually exists (count only real cells)."""
    return [
        name
        for name in fields
        if (WeatherRow.model_fields[name].alias or name) in fieldnames
    ]


def _load_one(path: Path) -> tuple[list[WeatherRow], int, int, int, int, int, list[str]]:
    rows: list[WeatherRow] = []
    dropped = 0
    notes: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        for lineno, raw in enumerate(reader, start=2):  # line 2 = first data row
            try:
                rows.append(WeatherRow.model_validate(raw))
            except ValidationError as exc:
                dropped += 1
                notes.append(
                    f"{path.name}: dropped row at line {lineno} ({exc.error_count()} error(s))"
                )

    # The water balance is an order-dependent running sum -> always return sorted.
    rows.sort(key=lambda r: r.timestamp)

    present_numeric = _present(_NUMERIC_FIELDS, fieldnames)
    present_critical = _present(SPRAY_CRITICAL_FIELDS, fieldnames)
    nan_cells = sum(1 for r in rows for name in present_numeric if getattr(r, name) is None)
    spray_critical_nan = sum(
        1 for r in rows for name in present_critical if getattr(r, name) is None
    )
    gap_count, max_gap = _detect_gaps([r.timestamp for r in rows])
    if gap_count:
        notes.append(f"{path.name}: {gap_count} missing hour(s) (longest run {max_gap}h)")
    if spray_critical_nan:
        notes.append(
            f"{path.name}: {spray_critical_nan} missing spray-critical cell(s) "
            "(Delta T / wind) -> those hours are not sprayable"
        )
    return rows, dropped, nan_cells, spray_critical_nan, gap_count, max_gap, notes


def _assess_staleness(
    history: list[WeatherRow],
    forecast: list[WeatherRow],
    run_date: date,
    threshold_hours: int,
) -> tuple[bool, float, bool, list[str]]:
    """Return (is_stale, staleness_hours, forecast_covers_tomorrow, notes).

    is_stale is strictly about HISTORY age; forecast coverage is a separate signal so the
    two don't get conflated (an agent reading staleness_hours alongside is_stale).
    """
    notes: list[str] = []
    if not history:
        return True, 0.0, False, ["no history rows to anchor freshness on"]

    anchor = max(r.timestamp for r in history)  # freshest observed hour
    run_dt = datetime.combine(run_date, time(0, 0))
    staleness_h = max(0.0, (run_dt - anchor).total_seconds() / 3600)
    is_stale = staleness_h > threshold_hours
    if is_stale:
        notes.append(
            f"freshest history row {anchor.isoformat()} is ~{staleness_h:.0f}h "
            f"before run_date {run_date.isoformat()} (> {threshold_hours}h) -> stale"
        )

    tomorrow = run_date + timedelta(days=1)
    fc_max = max((r.timestamp for r in forecast), default=None)
    covers_tomorrow = fc_max is not None and fc_max.date() >= tomorrow
    if not covers_tomorrow:
        notes.append(f"forecast does not cover tomorrow ({tomorrow.isoformat()})")

    return is_stale, round(staleness_h, 1), covers_tomorrow, notes


def load_weather(
    history_path: str | Path,
    forecast_path: str | Path,
    run_date: date,
    *,
    staleness_threshold_hours: int = 48,
) -> tuple[list[WeatherRow], list[WeatherRow], DataQuality]:
    """Load + validate both CSVs and return (history_rows, forecast_rows, DataQuality).

    Degradation policy (per consumer — see comments): short ET₀ gaps are safe to
    interpolate downstream (they only perturb a running sum), but a missing
    spray-critical hour (Delta T / wind) must be *excluded*, never fabricated into a
    sprayable window. Staleness / missing cells lower confidence rather than failing.
    """
    h = _load_one(Path(history_path))
    f = _load_one(Path(forecast_path))
    h_rows, h_drop, h_nan, h_crit, h_gap, h_maxgap, h_notes = h
    f_rows, f_drop, f_nan, f_crit, f_gap, f_maxgap, f_notes = f

    is_stale, staleness_h, covers_tomorrow, s_notes = _assess_staleness(
        h_rows, f_rows, run_date, staleness_threshold_hours
    )

    dq = DataQuality(
        rows_loaded=len(h_rows) + len(f_rows),
        rows_dropped=h_drop + f_drop,
        gap_count=h_gap + f_gap,
        max_gap_hours=max(h_maxgap, f_maxgap),
        nan_cells=h_nan + f_nan,
        spray_critical_nan_cells=h_crit + f_crit,
        is_stale=is_stale,
        staleness_hours=staleness_h,
        forecast_covers_tomorrow=covers_tomorrow,
        notes=h_notes + f_notes + s_notes,
    )
    return h_rows, f_rows, dq


# ---------------------------------------------------------------------------
# The seam: one quality assessment, shared by every source
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeatherLoadResult:
    """History + forecast + the quality verdict, from any source.

    The return type of the `WeatherSource` seam (sources/base.py). A source's
    only job is to produce `WeatherRow`s; whether the day is stale, gappy, or
    missing cells is not its to decide -- `assemble_load_result` decides that,
    the same way for every source, which is the whole point of ADR-002.
    """

    history: list[WeatherRow]
    forecast: list[WeatherRow]
    quality: DataQuality


def _count_missing(rows: list[WeatherRow], fields: tuple[str, ...]) -> int:
    return sum(1 for r in rows for name in fields if getattr(r, name) is None)


def assemble_load_result(
    history: list[WeatherRow],
    forecast: list[WeatherRow],
    run_date: date,
    *,
    staleness_threshold_hours: int = 48,
    rows_dropped: int = 0,
    extra_notes: tuple[str, ...] = (),
) -> WeatherLoadResult:
    """Assess already-loaded rows for quality and wrap them in a result.

    The shared assessment path a live source (Open-Meteo) ends with, computing
    gaps, staleness, and missing-cell penalties from in-memory rows with the
    *same* helpers `load_weather` uses for CSVs (`_detect_gaps`,
    `_assess_staleness`, `DataQuality`). `rows_dropped` is 0 for an API source
    (no parse step drops rows); `extra_notes` is where a source labels itself
    (e.g. "open-meteo has no spray index").
    """
    history = sorted(history, key=lambda r: r.timestamp)
    forecast = sorted(forecast, key=lambda r: r.timestamp)

    nan_cells = _count_missing(history, _NUMERIC_FIELDS) + _count_missing(forecast, _NUMERIC_FIELDS)
    spray_critical = _count_missing(history, SPRAY_CRITICAL_FIELDS) + _count_missing(
        forecast, SPRAY_CRITICAL_FIELDS
    )
    h_gap, h_maxgap = _detect_gaps([r.timestamp for r in history])
    f_gap, f_maxgap = _detect_gaps([r.timestamp for r in forecast])

    is_stale, staleness_h, covers_tomorrow, s_notes = _assess_staleness(
        history, forecast, run_date, staleness_threshold_hours
    )

    return WeatherLoadResult(
        history=history,
        forecast=forecast,
        quality=DataQuality(
            rows_loaded=len(history) + len(forecast),
            rows_dropped=rows_dropped,
            gap_count=h_gap + f_gap,
            max_gap_hours=max(h_maxgap, f_maxgap),
            nan_cells=nan_cells,
            spray_critical_nan_cells=spray_critical,
            is_stale=is_stale,
            staleness_hours=staleness_h,
            forecast_covers_tomorrow=covers_tomorrow,
            notes=list(extra_notes) + s_notes,
        ),
    )
