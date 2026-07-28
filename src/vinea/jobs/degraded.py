"""A complete advisory with no model call. The boundary, made persistable.

the core's CLI already proves the deterministic core can stand on its own -- run it
with no API key and it prints a full report. phase 8 needs that same report as a stored
`DailyFarmAdvisory`, flagged `degraded=true` on the row, for two situations:

  1. No API key configured (S3.5). The worker can't run the agents, but the grower
     still needs an answer, and the numbers are all there.
  2. The router judged the day too clear-cut to spend a model on (S3.6). Same
     deterministic answer, reached on purpose rather than by necessity.

Every field here is Python. The rationale/summary strings -- the fields the LLM
normally writes -- are filled with deterministic, templated explanations, not
prose. That's the honest thing: a degraded advisory should read as "here are the
numbers and the rules that produced them", not impersonate the judgement layer
that didn't run. `degraded=true` on the row is the machine-readable version of the
same admission.

This is emphatically NOT a second implementation of the agents. It computes no new
numbers -- it reads the ones `features.py` already produced and the conflict facts
`reconcile.py` already derives, and assembles them into the contract shape. The
irrigation *amount* is the one genuine decision, and it's a water-balance identity
(refill the depletion back to field capacity), not a judgement call.
"""

from __future__ import annotations

from vinea.contracts import (
    DailyFarmAdvisory,
    FarmFeatures,
    IrrigationAdvice,
    SprayAdvice,
)
from vinea.deps import Deps
from vinea.ingest import WeatherRow
from vinea.reconcile import build_conflict_facts


def _degraded_confidence(features: FarmFeatures) -> float:
    """Confidence for a no-model advisory: the data-quality ceiling alone.

    With no LLM signal to combine, confidence is exactly what the data supports
    and no more -- `1 - penalty`, the same ceiling the agents clamp the model to.
    A degraded advisory is never *more* confident than a model-backed one on the
    same data, which is the correct ordering: less information should not read as
    more certainty.
    """
    return 1.0 - features.data_quality.confidence_penalty


def build_degraded_advisory(
    features: FarmFeatures, forecast: list[WeatherRow], deps: Deps
) -> DailyFarmAdvisory:
    """Assemble a complete DailyFarmAdvisory from features, with no model call.

    `forecast` and `deps` are needed only to reuse `reconcile.build_conflict_facts`
    -- the same deterministic cross-leg facts the Coordinator would have been
    handed -- so the degraded plan is grounded in exactly what a model run would
    have seen, minus the model.
    """
    irr_f = features.irrigation
    confidence = _degraded_confidence(features)
    triggered = irr_f.should_irrigate_trigger

    # The amount is a water-balance identity, not a judgement: to bring the root
    # zone back to field capacity you replace exactly the depletion. Only
    # recommend it when the mechanical trigger has actually fired.
    recommended_depth = round(irr_f.current_depletion_mm, 1) if triggered else None

    irrigation = IrrigationAdvice(
        target_date=features.target_date,
        should_irrigate_tomorrow=triggered,
        recommended_depth_mm=recommended_depth,
        current_depletion_mm=irr_f.current_depletion_mm,
        confidence=confidence,
        rationale=(
            f"Deterministic (no model): depletion {irr_f.current_depletion_mm:.1f}mm "
            f"{'>=' if triggered else '<'} RAW {irr_f.raw_mm:.1f}mm, so "
            f"{'irrigate to refill the deficit' if triggered else 'no irrigation needed'}."
            + (f" Note: {irr_f.notes[0]}" if irr_f.notes else "")
        ),
    )

    windows = list(features.spray.windows)
    can_spray = bool(windows)
    limiting = (
        []
        if can_spray
        else (list(features.spray.limiting_factors) or ["no hour cleared all spray gates"])
    )
    spray = SprayAdvice(
        target_date=features.target_date,
        can_spray_tomorrow=can_spray,
        # No trimming judgement without a model: the candidate windows ARE the
        # answer. Every one already cleared all four physical gates.
        recommended_windows=windows if can_spray else [],
        limiting_factors=limiting,
        confidence=confidence,
        rationale=(
            f"Deterministic (no model): {len(windows)} candidate window(s) cleared all four "
            f"gates; presented without further selection."
            if can_spray
            else "Deterministic (no model): no hour cleared all four spray gates."
        ),
    )

    conflict_facts = build_conflict_facts(forecast, irrigation, spray, deps, features.target_date)
    if triggered and can_spray:
        plan = (
            "Irrigate and spray can both proceed; drip wets the root zone, not the canopy, so "
            "they do not interact. Order by convenience."
        )
    elif triggered:
        plan = "Irrigate to refill the deficit. No safe spray window today."
    elif can_spray:
        plan = "No irrigation needed. Spray within a candidate window."
    else:
        plan = "No irrigation needed and no safe spray window today; hold."

    return DailyFarmAdvisory(
        date=features.target_date,
        irrigation=irrigation,
        spray=spray,
        summary=plan,
        conflicts_resolved=conflict_facts,
        # A reconciliation can't be more confident than its legs; with equal
        # deterministic confidence, it equals them.
        overall_confidence=confidence,
    )
