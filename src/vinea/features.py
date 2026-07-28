"""FeatureBuilder — the deterministic agronomy (the LLM/deterministic boundary).

Python computes the physics exactly and reproducibly; the phase 3 agents only *judge and
explain* over these numbers. No LLM calls, no I/O — pure functions over validated rows.

  Irrigation (#3): ETc = ET₀ × Kc, running soil-water balance -> current depletion.
  Spray      (#4): Delta-T bands + wind + rain-fastness + Spray-Index -> spray windows.

All thresholds come from the injected `Deps` (deps.py) — no magic numbers here.
TODO(phase 3 #10): invoke these from a non-agent `FeatureBuilder(BaseNode)` whose `run` returns
the next node and stashes `FarmFeatures` on `GraphRunContext.state`.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta

from .contracts import (
    Band,
    FarmFeatures,
    HourClassification,
    IrrigationFeatures,
    SprayFeatures,
    SprayWindow,
    WindClass,
)
from .deps import Deps
from .ingest import DataQuality, WeatherRow


# --- irrigation: ETc + running water balance (FAO-56) --------------------------

def build_irrigation_features(
    history: list[WeatherRow],
    forecast: list[WeatherRow],
    deps: Deps,
    run_date: date,
) -> IrrigationFeatures:
    kc, taw, raw = deps.kc, deps.taw_mm, deps.raw_mm
    eff_frac = deps.effective_rain_fraction

    depletion = deps.initial_depletion_mm  # t=0 assumption (soil at field capacity)
    cumulative_etc = 0.0
    skipped = 0
    for r in history:  # ingest returns rows sorted by timestamp
        if r.et0_mm is None:
            skipped += 1  # don't zero-fill: that would silently understate demand
            continue
        etc = r.et0_mm * kc
        cumulative_etc += etc
        eff_rain = (r.precip_mm or 0.0) * eff_frac
        # depletion grows with demand, shrinks with effective rain; clamp to [0, TAW].
        depletion = min(max(depletion + etc - eff_rain, 0.0), taw)

    current_depletion = depletion

    tomorrow = run_date + timedelta(days=1)
    trows = [r for r in forecast if r.timestamp.date() == tomorrow]
    etc_tomorrow = 0.0
    skipped_fc = 0
    for r in trows:
        if r.et0_mm is None:
            skipped_fc += 1  # same policy as history: skip, don't zero-fill
            continue
        etc_tomorrow += r.et0_mm * kc
    rain_tomorrow = sum((r.precip_mm or 0.0) for r in trows)
    eff_rain_tomorrow = rain_tomorrow * eff_frac
    projected = min(max(current_depletion + etc_tomorrow - eff_rain_tomorrow, 0.0), taw)

    # Mechanical RAW/MAD trigger — the agent applies the deficit-irrigation judgement on top.
    trigger = (current_depletion >= raw) and (eff_rain_tomorrow < deps.rain_skip_mm)
    # current_depletion is already clamped to [0, TAW]; refill toward field capacity (* deficit knob).
    depth = round(current_depletion * deps.refill_fraction, 1) if trigger else None

    notes: list[str] = []
    if skipped:
        notes.append(f"{skipped} history hour(s) with missing ET₀ were skipped (not zero-filled)")
    if skipped_fc:
        notes.append(f"{skipped_fc} tomorrow forecast hour(s) with missing ET₀ were skipped (not zero-filled)")
    if not trows:
        notes.append(f"no forecast rows for tomorrow ({tomorrow.isoformat()}) — projection uses current only")
    if history:
        # balance ends at the freshest observation; the gap to run_date is flagged by DataQuality.
        notes.append(f"depletion is as of last history hour {history[-1].timestamp.isoformat()} (not carried forward to run_date)")
    if current_depletion >= taw:
        notes.append(
            "depletion (and thus recommended_depth_mm) clamped at TAW: raw demand exceeds TAW because the "
            "dataset has no irrigation log; treat as an upper-bound atmospheric-demand signal"
        )

    return IrrigationFeatures(
        crop=deps.crop, kc=kc, taw_mm=taw, raw_mm=raw, mad_fraction=deps.mad_fraction,
        initial_depletion_mm=deps.initial_depletion_mm,
        effective_rain_fraction=eff_frac, rain_skip_mm=deps.rain_skip_mm,
        refill_fraction=deps.refill_fraction,
        cumulative_etc_mm=round(cumulative_etc, 2),
        current_depletion_mm=round(current_depletion, 2),
        etc_tomorrow_mm=round(etc_tomorrow, 2),
        forecast_rain_tomorrow_mm=round(rain_tomorrow, 2),
        effective_rain_tomorrow_mm=round(eff_rain_tomorrow, 2),
        projected_depletion_mm=round(projected, 2),
        should_irrigate_trigger=trigger,
        recommended_depth_mm=depth,
        skipped_et0_hours=skipped,
        notes=notes,
    )


# --- spray: Delta-T bands + wind + window detection ----------------------------

def classify_delta_t(dt: float | None, deps: Deps) -> Band | None:
    """BoM bands: <2 inversion · 2-8 ideal · 8-10 marginal · >10 unsuitable."""
    if dt is None:
        return None
    lo, hi = deps.deltat_ideal
    if dt < deps.deltat_inversion_below:
        return "inversion_risk"
    if lo <= dt <= hi:
        return "ideal"
    if hi < dt <= deps.deltat_marginal_upper:
        return "marginal"
    return "unsuitable"


def classify_wind(ws: float | None, deps: Deps) -> WindClass | None:
    if ws is None:
        return None
    lo, hi = deps.wind_ideal_ms
    if ws < lo:
        return "near_calm"
    if ws > hi:
        return "drift"
    return "ok"


def _gate_reason(hc: HourClassification, deps: Deps) -> str:
    """First failing gate of an unsuitable hour, citing the real value/column."""
    lo, hi = deps.wind_ideal_ms
    if hc.band == "unsuitable":
        return f"Delta T {hc.delta_t_c:.2f} °C > {deps.deltat_marginal_upper} → unsuitable"
    if hc.band == "inversion_risk":
        return f"Delta T {hc.delta_t_c:.2f} °C < {deps.deltat_inversion_below} → inversion risk"
    if hc.band is None:
        return "missing Delta T → not sprayable"
    if hc.wind_class == "drift":
        return f"wind {hc.wind_ms:.2f} m/s > {hi} m/s → drift"
    if hc.wind_class == "near_calm":
        return f"wind {hc.wind_ms:.2f} m/s < {lo} m/s → near-calm/inversion"
    if hc.wind_class is None:
        return "missing wind → not sprayable"
    if not hc.rain_fast_ok:
        return f"rain within {deps.rain_fast_hours}h → rain-fastness"
    if not hc.index_ok:
        op = "<" if deps.spray_index_higher_is_better else ">"
        return f"Spray Index {hc.spray_index} {op} {deps.spray_index_cutoff} cutoff"
    return "suitable"


def _classify_hours(rows: list[WeatherRow], deps: Deps) -> list[HourClassification]:
    rows = sorted(rows, key=lambda r: r.timestamp)
    out: list[HourClassification] = []
    for i, r in enumerate(rows):
        band = classify_delta_t(r.delta_t_c, deps)
        wind_class = classify_wind(r.wind_ms, deps)
        # rain-fastness: dry now AND for the next rain_fast_hours. Time-based (gap-safe) and
        # crosses midnight when the caller passes a lookahead buffer into the next day.
        horizon = r.timestamp + timedelta(hours=deps.rain_fast_hours)
        lookahead = [w for w in rows[i:] if w.timestamp <= horizon]
        rain_fast_ok = all((w.precip_mm or 0.0) <= 0.0 for w in lookahead)
        index_ok = (
            r.spray_index is None
            or (r.spray_index >= deps.spray_index_cutoff if deps.spray_index_higher_is_better
                else r.spray_index <= deps.spray_index_cutoff)
        )
        suitable = (
            band in ("ideal", "marginal")
            and wind_class == "ok"
            and rain_fast_ok
            and index_ok
        )
        hc = HourClassification(
            timestamp=r.timestamp, delta_t_c=r.delta_t_c, wind_ms=r.wind_ms,
            spray_index=r.spray_index, precip_mm=r.precip_mm, vpd_kpa=r.vpd_kpa,
            band=band, wind_class=wind_class, rain_fast_ok=rain_fast_ok,
            index_ok=index_ok, suitable=suitable,
        )
        if not suitable:
            hc.gate_failures.append(_gate_reason(hc, deps))
        out.append(hc)
    return out


def detect_spray_windows(hours: list[HourClassification], deps: Deps) -> list[SprayWindow]:
    """Merge consecutive suitable hours into windows; `end` is exclusive (+1h)."""
    windows: list[SprayWindow] = []
    n = len(hours)
    i = 0
    while i < n:
        if not hours[i].suitable:
            i += 1
            continue
        j = i
        while j + 1 < n and hours[j + 1].suitable:
            j += 1
        bands = Counter(h.band for h in hours[i : j + 1])
        reason = f"{j - i + 1}h suitable (Delta T {', '.join(f'{b}×{c}' for b, c in bands.items())}; wind ok; rain-fast)"
        limiting: list[str] = []
        if i > 0:  # what gate bounded the window on the left
            limiting.append(f"before {hours[i].timestamp:%H:%M}: {_gate_reason(hours[i - 1], deps)}")
        if j + 1 < n:  # ... and on the right
            limiting.append(f"after {hours[j].timestamp:%H:%M}: {_gate_reason(hours[j + 1], deps)}")
        windows.append(SprayWindow(
            start=hours[i].timestamp,
            end=hours[j].timestamp + timedelta(hours=1),  # exclusive end
            reason=reason,
            limiting_factors=limiting,
        ))
        i = j + 1
    return windows


def spray_features_for_tomorrow(
    forecast: list[WeatherRow], deps: Deps, run_date: date
) -> SprayFeatures:
    tomorrow = run_date + timedelta(days=1)
    day_after = tomorrow + timedelta(days=1)
    # Classify tomorrow PLUS a buffer into the next day so rain-fastness can see across midnight,
    # then keep only tomorrow's hours for windows/output.
    considered = [r for r in forecast if tomorrow <= r.timestamp.date() <= day_after]
    classified = _classify_hours(considered, deps)
    hours = [h for h in classified if h.timestamp.date() == tomorrow]
    windows = detect_spray_windows(hours, deps)
    band_counts = {b: c for b, c in Counter(h.band for h in hours if h.band).items()}

    limiting: list[str] = []
    if not windows:
        # Summarize the distinct reasons no window opened (mask numbers so e.g. all wind-drift
        # hours collapse to one line regardless of the exact speed).
        seen, uniq = set(), []
        for h in hours:
            if not h.suitable and h.gate_failures:
                rsn = h.gate_failures[0]
                k = re.sub(r"[-+]?\d[\d.]*", "#", rsn)
                if k not in seen:
                    seen.add(k)
                    uniq.append(rsn)
        limiting = uniq or [f"no forecast rows for tomorrow ({tomorrow.isoformat()})"]

    return SprayFeatures(
        target_date=tomorrow,
        can_spray=bool(windows),
        windows=windows,
        hours=hours,
        limiting_factors=limiting,
        band_counts=band_counts,
    )


# --- aggregate -----------------------------------------------------------------

def build_features(
    history: list[WeatherRow],
    forecast: list[WeatherRow],
    data_quality: DataQuality,
    run_date: date,
    deps: Deps,
) -> FarmFeatures:
    return FarmFeatures(
        as_of=run_date,
        target_date=run_date + timedelta(days=1),
        data_quality=data_quality,
        irrigation=build_irrigation_features(history, forecast, deps, run_date),
        spray=spray_features_for_tomorrow(forecast, deps, run_date),
    )
