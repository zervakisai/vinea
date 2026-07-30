"""Injected crop/agronomy configuration (the `deps_type` source of truth).

Every tunable (Kc, root zone, irrigation trigger, Delta-T / wind bands, Spray-Index
direction & cutoff) lives here as one typed object, NOT as magic numbers in feature code
or prompt strings. This is what lets the *same* graph serve a different crop/region by
passing a different `Deps` — the brief's "crop and its parameters must be INJECTED" and
the later phases' claim that "a new crop = a config change, not a model change."

Frozen + slots: immutable deps are safe to share across concurrent async runs (the later phases)
and act as a single source of truth, so the number Python computes with and the number the
LLM sees can never drift apart.

This object is passed via `Agent(deps_type=Deps)` / `graph.run(..., deps=...)` and
read through `RunContext[Deps].deps` / `GraphRunContext.deps`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Deps:
    crop: str = "wine grapes (Vitis vinifera)"
    irrigation_method: str = "drip"        # drip wets the root zone, not the canopy (reconcile)
    spray_sensitivity: str = "very high (powdery/downy mildew programme all summer)"

    # --- Irrigation (FAO-56) ---
    kc: float = 0.70                       # mid-season crop coefficient (FAO-56 Table 12, wine grapes)
    root_depth_m: float = 1.2             # mature vine root zone (1.0-1.5 m)
    taw_mm: float = 150.0                 # total available water in root zone (~0.125 * 1200 mm loam)
    mad_fraction: float = 0.45            # management-allowed depletion p; RAW = p * TAW
    initial_depletion_mm: float = 0.0     # t=0 assumption: soil at field capacity at history start
    effective_rain_fraction: float = 0.80 # gross rain * fraction = effective (rest lost to runoff/interception)
    rain_skip_mm: float = 5.0             # skip irrigation if effective forecast rain >= this (mm)
    refill_fraction: float = 1.0          # 1.0 = refill to field capacity; <1.0 = deficit-irrigation knob

    # --- Spray (BoM Delta T + wind) ---
    deltat_ideal: tuple[float, float] = (2.0, 8.0)   # °C: 2<=dt<=8 ideal
    deltat_marginal_upper: float = 10.0              # °C: 8<dt<=10 marginal; >10 unsuitable
    deltat_inversion_below: float = 2.0              # °C: dt<2 inversion risk
    wind_ideal_ms: tuple[float, float] = (0.83, 4.2) # m/s: BoM 3-15 km/h ideal band
    spray_index_higher_is_better: bool = True        # provided 0-100 score; higher = more suitable
    spray_index_cutoff: float = 40.0                 # drop hours below this (physics can still override)
    rain_fast_hours: int = 2                         # dry hours needed after application (rain-fastness)

    @property
    def raw_mm(self) -> float:
        """Readily available water = MAD trigger depth (mm)."""
        return self.mad_fraction * self.taw_mm


# The reference crop for this project.
WINE_GRAPES = Deps()

# TODO: a new crop/region is a ONE-OBJECT change — no code/prompt edits. e.g.:
#   TABLE_GRAPES = Deps(crop="table grapes", kc=0.85, root_depth_m=1.0)
# TODO: in production, load a CropConfig from a registry/YAML by (crop, region).
