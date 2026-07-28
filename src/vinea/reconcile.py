"""Deterministic conflict-detection helpers for the Coordinator.

The Coordinator LLM *reconciles*; it must NOT recompute physics. So the cross-domain
facts it reasons over (daylight bounds, spray↔irrigation overlap, rain-fastness, canopy
wetting) are derived here in plain Python and handed over as clean strings — keeping the
LLM/deterministic boundary intact even inside the reconcile step.
"""

from __future__ import annotations

from datetime import date, time, timedelta

from .contracts import IrrigationAdvice, SprayAdvice
from .deps import Deps
from .ingest import WeatherRow


def daylight_bounds(forecast: list[WeatherRow], target_date: date) -> tuple[time, time] | None:
    """Sunrise/sunset for `target_date` from GHI (first/last hour with irradiance > 0)."""
    lit = [
        r.timestamp.time()
        for r in forecast
        if r.timestamp.date() == target_date and r.ghi_wm2 is not None and r.ghi_wm2 > 0
    ]
    if not lit:
        return None
    return min(lit), max(lit)


def build_conflict_facts(
    forecast: list[WeatherRow],
    irrigation: IrrigationAdvice,
    spray: SprayAdvice,
    deps: Deps,
    target_date: date,
) -> list[str]:
    """Cross-domain facts (grounded, deterministic) for the Coordinator to reconcile."""
    facts: list[str] = []

    bounds = daylight_bounds(forecast, target_date)
    if bounds:
        sunrise, sunset = bounds
        facts.append(
            f"daylight inferred from GHI coverage ~{sunrise:%H:%M}–{sunset:%H:%M}; "
            f"irrigation suggested after sunset (~{sunset:%H:%M})"
        )
    else:
        facts.append("daylight bounds unavailable (no GHI) — cannot ground 'after sunset' precisely")

    # Temporal overlap between any spray window and the after-sunset irrigation period.
    # A window overlaps if it starts before sunrise, ends after sunset, or rolls past midnight.
    if bounds and spray.recommended_windows:
        sunrise, sunset = bounds
        overlap = any(
            w.start.time() < sunrise or w.end.time() > sunset or w.end.date() > target_date
            for w in spray.recommended_windows
        )
        facts.append(
            "a spray window reaches outside daylight, near the after-sunset irrigation period → review sequencing"
            if overlap
            else "spray windows do not reach the after-sunset irrigation period → no temporal overlap"
        )

    # Rain-fastness: only rain within `rain_fast_hours` AFTER a window end is a conflict.
    horizon = timedelta(hours=deps.rain_fast_hours)
    rain_after = any(
        (r.precip_mm or 0.0) > 0.0
        for w in spray.recommended_windows
        for r in forecast
        if w.end <= r.timestamp <= w.end + horizon
    )
    if spray.recommended_windows:
        facts.append(
            f"forecast rain within {deps.rain_fast_hours}h after a spray window → rain-fastness risk"
            if rain_after
            else f"0 mm forecast rain within {deps.rain_fast_hours}h after spray windows → no rain-fastness conflict"
        )

    # Canopy wetting interaction depends on irrigation method.
    if deps.irrigation_method == "drip":
        facts.append("drip irrigation wets the root zone, not the canopy → no fungicide washoff / leaf-wetness conflict")
    else:
        facts.append(f"{deps.irrigation_method} irrigation may wet the canopy → consider washoff/leaf-wetness vs spray timing")

    # TODO: a richer canopy-microclimate model (after-sunset humidity raising overnight
    #       leaf wetness / lowering Delta T) would refine the spray↔irrigation interaction.
    return facts
