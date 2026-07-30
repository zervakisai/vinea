"""Retrieval queries built from tonight's numbers, not baked at import.

The first version of this was two module-level strings. That made retrieval a
constant: the same query, every night, for every tenant, against a static corpus,
returning the same three passages forever. The whole pgvector/hybrid/RRF apparatus
was an expensive way to look up a value that could have been pasted into the
prompt at build time — and nothing about the retrieval could ever be *wrong*,
which is another way of saying it could never be useful either.

Queries are built here from the deterministic features, so a night where the root
zone is past the refill trigger asks about irrigation scheduling under stress, and
a night where rain is forecast asks about effective rainfall instead. The corpus
is a 300-page agronomy manual; different nights genuinely need different pages.

Two rules the construction obeys:

  **Deterministic.** Same features in, same query out. Retrieval must not become
  a source of run-to-run variance on top of the model's own -- an advisory that
  differs from yesterday's should differ because the weather did.

  **The document's vocabulary, not the grower's.** The lexical half of the hybrid
  matches tokens, and FAO-56 indexes on "readily available water" and "depletion
  fraction", not "should I water tomorrow". Phrasing the query as a grower would
  speak retrieves the introduction of every chapter and the substance of none.
"""

from __future__ import annotations

from vinea.contracts import IrrigationFeatures, SprayFeatures

# The terms every irrigation query carries, whatever the night looks like. These
# name the quantities `features.py` actually computes, so the retrieved passages
# are about the same physics the numbers came from.
_IRRIGATION_BASE = (
    "soil water balance of the root zone, total available water TAW, "
    "readily available water RAW, depletion fraction"
)

_SPRAY_BASE = "weather conditions affecting field operations and spray droplet evaporation"


def irrigation_query(features: IrrigationFeatures) -> str:
    """What to ask FAO-56 about tonight's water balance.

    The branches follow the decision the advisory is actually making, in the
    order the water balance makes it: is meaningful rain coming, is the root zone
    past its trigger, or is there comfortable margin left.
    """
    parts = [_IRRIGATION_BASE]

    if features.effective_rain_tomorrow_mm >= features.rain_skip_mm > 0:
        # Rain changes the question from "how much water" to "how much of this
        # rain actually reaches the roots".
        parts.append(
            "effective rainfall, infiltration and runoff, deep percolation below the root zone"
        )
    if features.current_depletion_mm >= features.raw_mm:
        # Past the trigger: the relevant chapter is water stress, not scheduling.
        parts.append(
            "water stress coefficient Ks when depletion exceeds readily available water, "
            "reduced crop evapotranspiration under soil water stress"
        )
    else:
        parts.append(
            "irrigation scheduling to avoid crop water stress, net irrigation depth to apply"
        )
    if features.skipped_et0_hours:
        # The advisory will carry a data-quality caveat; give the model the
        # document's own guidance on estimating ETo from incomplete data.
        parts.append("estimating missing meteorological data and reference evapotranspiration")

    return ", ".join(parts)


def spray_query(features: SprayFeatures) -> str:
    """What to ask FAO-56 about tonight's spray window.

    Driven by the deterministic gate's own `limiting_factors`: those strings are
    the reason a window was refused, and they are the thing the rationale has to
    explain. When nothing is limiting, the question becomes why the conditions
    are suitable rather than why they are not.
    """
    parts = [_SPRAY_BASE]
    limiting = " ".join(features.limiting_factors).lower()

    if "wind" in limiting:
        parts.append("wind speed measurement, wind profile and adjustment to two metres height")
    if "delta" in limiting or "delta t" in limiting:
        parts.append(
            "vapour pressure deficit, wet bulb temperature, relative humidity and evaporative demand"
        )
    if "rain" in limiting:
        parts.append("precipitation and wetting events, evaporation from a wet canopy")
    if not features.windows:
        parts.append("diurnal variation of temperature humidity and wind through the day")
    if len(parts) == 1:
        # Nothing was limiting: the interesting question is what makes an hour
        # suitable, which is the same physics read from the other side.
        parts.append("temperature humidity and wind conditions during the day, dew point")

    return ", ".join(parts)
