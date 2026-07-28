"""The deterministic-feature router. An `if`, not a second model call.

DESIGN.md B1: most of a night's runs don't need a frontier model's judgement, and
the deterministic features already say which. A depletion nowhere near the RAW
trigger, with a forecast that has no candidate spray windows, is not a day anyone
needs an LLM to reason about -- the answer is "do nothing", and Python already
knows that. Route only the *genuinely borderline* days -- depletion near the
trigger, windows that only just exist -- to the large model.

The critical property, enforced here: the router reads the same `FarmFeatures`
object the agents do, and it is a comparison, not a model call. It costs nothing,
it's fully testable, and it can't hallucinate. If routing were itself an LLM call,
you'd have spent a model to decide whether to spend a model -- and inherited its
non-determinism at the very point the system is trying to be predictable.

Why the asymmetry lands the way it does: skipping the model on a clear-cut day
routes it to `build_degraded_advisory`, which is deterministic and correct for
exactly the days the router selects (nothing to weigh, nothing to phrase). The
risk of skipping is therefore not "a wrong number" -- the numbers are Python
either way -- but "a curt explanation where a nuanced one would have helped".
That's an acceptable trade on a day where the deterministic answer is unambiguous,
and the router's whole job is to only make that trade there.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from vinea.contracts import FarmFeatures


class Route(enum.StrEnum):
    """Where a day's advisory should be produced."""

    # Deterministic answer only, no model. The day is clear-cut in both legs.
    SKIP_MODEL = "skip_model"
    # Borderline: worth a model's judgement and phrasing.
    LARGE_MODEL = "large_model"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: Route
    reason: str


# How close to the RAW trigger counts as "borderline" and worth a model.
# Expressed as a fraction of RAW so it scales with the crop's own thresholds
# rather than being an absolute mm value baked in here (a threshold living outside
# Deps -- the thing this repo refuses).
BORDERLINE_FRACTION_OF_RAW = 0.15


def route_for(features: FarmFeatures) -> RouteDecision:
    """Decide whether this day needs the large model, from the features alone.

    A day is routed to the model if EITHER leg is borderline:

      - irrigation is borderline when depletion is within
        BORDERLINE_FRACTION_OF_RAW of the RAW trigger (either side) -- the region
        where "irrigate or not" is a genuine call rather than obvious.
      - spray is borderline when there is at least one candidate window: which
        window to recommend, and how to caveat it, is judgement and phrasing the
        model adds value to.

    Otherwise -- depletion comfortably clear of the trigger AND no spray window to
    discuss -- the day is clear-cut and skips the model.
    """
    irr = features.irrigation
    distance_to_trigger = abs(irr.current_depletion_mm - irr.raw_mm)
    band = BORDERLINE_FRACTION_OF_RAW * irr.raw_mm

    irrigation_borderline = distance_to_trigger <= band
    spray_borderline = len(features.spray.windows) > 0

    if irrigation_borderline and spray_borderline:
        reason = (
            f"both legs borderline: depletion {irr.current_depletion_mm:.1f}mm within "
            f"{band:.1f}mm of RAW {irr.raw_mm:.1f}mm, and "
            f"{len(features.spray.windows)} spray window(s) to weigh"
        )
        return RouteDecision(Route.LARGE_MODEL, reason)
    if irrigation_borderline:
        return RouteDecision(
            Route.LARGE_MODEL,
            f"irrigation borderline: depletion {irr.current_depletion_mm:.1f}mm within "
            f"{band:.1f}mm of RAW {irr.raw_mm:.1f}mm",
        )
    if spray_borderline:
        return RouteDecision(
            Route.LARGE_MODEL,
            f"spray borderline: {len(features.spray.windows)} candidate window(s) to weigh",
        )

    return RouteDecision(
        Route.SKIP_MODEL,
        f"clear-cut: depletion {irr.current_depletion_mm:.1f}mm is {distance_to_trigger:.1f}mm from "
        f"RAW {irr.raw_mm:.1f}mm (band {band:.1f}mm) and there are no candidate spray windows",
    )
