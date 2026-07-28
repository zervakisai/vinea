"""The three Pydantic AI agents — the bounded judgement-and-explanation layer.

Each agent has output_type=<typed contract>, deps_type=<crop config + the day's deterministic
features> (so dynamic instructions and output_validators can ground-check), one dynamic
@agent.instructions seam (where a registry-fetched prompt would land — B3), and one
@agent.output_validator that raises ModelRetry on ungrounded output (no invented numbers).

The agents NEVER recompute physics — they reason over the FeatureBuilder's clean numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pydantic_ai import Agent, ModelRetry, RunContext

from . import config
from .contracts import (
    DailyFarmAdvisory,
    IrrigationAdvice,
    IrrigationFeatures,
    Reconciliation,
    SprayAdvice,
    SprayFeatures,
)
from .deps import Deps
from .ingest import DataQuality


# --- agent deps: crop config + the day's features (so validators can ground-check) ---

@dataclass
class IrrDeps:
    crop: Deps
    features: IrrigationFeatures
    data_quality: DataQuality
    target_date: date
    run_date: date


@dataclass
class SprayDeps:
    crop: Deps
    features: SprayFeatures
    data_quality: DataQuality
    target_date: date
    run_date: date


@dataclass
class CoordDeps:
    crop: Deps
    irrigation: IrrigationAdvice
    spray: SprayAdvice
    conflict_facts: list[str]
    data_quality: DataQuality
    target_date: date
    run_date: date


def _dq_note(dq: DataQuality) -> str:
    if dq.is_stale or dq.rows_dropped or dq.nan_cells or dq.spray_critical_nan_cells:
        return f"DEGRADED ({'; '.join(dq.notes) or 'see DataQuality'}) → lower confidence and say so"
    return "OK"


def _degrade(confidence: float, dq: DataQuality) -> tuple[float, str | None]:
    """Bound confidence by a deterministic data-quality CEILING (1 - confidence_penalty) and
    surface a caveat. The ceiling means confidence can't outrun the evidence; the agent prompt
    also asks the model to lower it, but this is the enforced backstop."""
    ceiling = round(1.0 - dq.confidence_penalty, 3)
    caveat = None
    if dq.confidence_penalty > 0:  # caveat even when notes is empty (e.g. only non-critical NaN cells)
        caveat = "data quality: " + ("; ".join(dq.notes) if dq.notes else f"confidence capped at {ceiling}")
    return min(confidence, ceiling), caveat


# TODO(robustness — consciously cut, and how I'd add them):
#  - provider rate-limit retries/backoff, timeouts-as-deadlines, circuit breakers, cross-provider
#    fallback live in phase 8 runtime, not this project.
#  - property-based/fuzz tests + CSV schema-drift detection -> a separate hardening pass.
#  - CI: pytest + ruff gate in GitHub Actions (cut per brief).
#  - on retry-exhaustion the CLI degrades to deterministic features; a richer path would synthesize
#    a low-confidence advisory directly from the features.


# --- dynamic instruction renderers (pure fns so they're unit-testable) -----------

def render_irrigation_context(d: IrrDeps) -> str:
    c, f = d.crop, d.features
    return (
        f"Crop: {c.crop} ({c.irrigation_method}-irrigated). As of {d.run_date.isoformat()}, "
        f"advising for {d.target_date.isoformat()}. "
        f"Injected config: Kc={c.kc}, RAW/MAD trigger={c.raw_mm} mm, TAW={c.taw_mm} mm, "
        f"refill cap (field capacity)={c.taw_mm} mm. "
        f"Computed (DO NOT recompute): current_depletion_mm={f.current_depletion_mm}. "
        f"Data quality: {_dq_note(d.data_quality)}."
        # TODO(B3): swap this f-string for ctx.deps.registry.get('agronomy_advisor@production') + bundled fallback
    )


def render_spray_context(d: SprayDeps) -> str:
    c = d.crop
    lo, hi = c.deltat_ideal
    wlo, whi = c.wind_ideal_ms
    direction = "higher = more suitable" if c.spray_index_higher_is_better else "lower = more suitable"
    return (
        f"Crop: {c.crop}, spray sensitivity: {c.spray_sensitivity}. As of {d.run_date.isoformat()}, "
        f"advising for {d.target_date.isoformat()}. "
        f"Injected thresholds: Delta T ideal {lo}-{hi} °C, marginal {hi}-{c.deltat_marginal_upper} °C, "
        f">{c.deltat_marginal_upper} unsuitable, <{c.deltat_inversion_below} inversion; wind ideal "
        f"{wlo}-{whi} m/s; Spray Index {direction}, cutoff {c.spray_index_cutoff}. "
        f"Data quality: {_dq_note(d.data_quality)}."
        # TODO(B3): swap for registry-fetched prompt + bundled fallback
    )


def render_coordinator_context(d: CoordDeps) -> str:
    c = d.crop
    return (
        f"Crop: {c.crop}, {c.irrigation_method}-irrigated, deficit-irrigation strategy. As of "
        f"{d.run_date.isoformat()}, advising for {d.target_date.isoformat()}. Reconcile the two typed "
        f"sub-advices into one coherent, sequenced day; explain interactions in plain grower language. "
        f"Data quality: {_dq_note(d.data_quality)}."
        # TODO(B3): swap for registry-fetched prompt + bundled fallback
    )


# --- run input renderers (the variable numbers the agent reasons over) -----------

def render_irrigation_input(f: IrrigationFeatures, target_date: date) -> str:
    return (
        f"Decide irrigation for {target_date.isoformat()}. Deterministic features:\n"
        f"- current_depletion_mm: {f.current_depletion_mm} (clamped to [0, TAW={f.taw_mm}])\n"
        f"- RAW/MAD trigger: {f.raw_mm} mm\n"
        f"- cumulative ETc over history: {f.cumulative_etc_mm} mm\n"
        f"- tomorrow ETc demand: {f.etc_tomorrow_mm} mm\n"
        f"- forecast rain tomorrow: {f.forecast_rain_tomorrow_mm} mm (effective {f.effective_rain_tomorrow_mm} mm)\n"
        f"- mechanical should_irrigate_trigger: {f.should_irrigate_trigger}\n"
        f"- suggested refill depth: {f.recommended_depth_mm} mm\n"
        f"- notes: {'; '.join(f.notes) or 'none'}\n"
        f"Copy current_depletion_mm verbatim. Apply deficit-irrigation judgement and explain."
    )


def render_spray_input(sf: SprayFeatures) -> str:
    windows = (
        "\n".join(f"  - {w.start:%H:%M}-{w.end:%H:%M}: {w.reason}" for w in sf.windows)
        or "  (none)"
    )
    hours = "\n".join(
        f"  {h.timestamp:%H:%M} band={h.band} wind={h.wind_ms}m/s index={h.spray_index} "
        f"precip={h.precip_mm} suitable={h.suitable}"
        for h in sf.hours
    )
    return (
        f"Decide spraying for {sf.target_date.isoformat()}. Band counts: {sf.band_counts}.\n"
        f"Candidate windows (choose ONLY from these; you may trim):\n{windows}\n"
        f"Limiting factors: {'; '.join(sf.limiting_factors) or 'none'}\n"
        f"Per-hour signals:\n{hours}"
    )


def render_coordinator_input(d: CoordDeps) -> str:
    return (
        f"IrrigationAdvice (embed verbatim):\n{d.irrigation.model_dump_json(indent=2)}\n\n"
        f"SprayAdvice (embed verbatim):\n{d.spray.model_dump_json(indent=2)}\n\n"
        f"Deterministic conflict facts (use for conflicts_resolved):\n- "
        + "\n- ".join(d.conflict_facts)
    )


# --- static personas -------------------------------------------------------------

_IRR_STATIC = (
    "You are an irrigation advisor for a vineyard — a BOUNDED judgement-and-explanation layer over a "
    "deterministic FAO-56 water balance computed in Python. Never recompute or invent a number; copy "
    "current_depletion_mm verbatim from the features. Decide should_irrigate_tomorrow using the mechanical "
    "RAW/MAD trigger AND deficit-irrigation judgement (post-veraison mild stress is desirable for wine "
    "quality, so a marginally-crossed trigger may warrant a partial refill or holding off — say so). "
    "Remember the asymmetric cost: not irrigating when needed is costlier than a small over-irrigation. "
    "Cite real depletion/ETc/rain numbers in rationale and evidence; lower confidence on degraded data; "
    "set target_date to the given date. recommended_depth_mm must not exceed TAW."
)

_SPRAY_STATIC = (
    "You are a spray advisor for a VERY spray-sensitive vineyard — a bounded judgement-and-explanation layer "
    "over deterministic Delta-T/wind/rain-fastness gating. Choose recommended_windows ONLY from the provided "
    "candidates (you may trim, never invent), and never recompute bands. Explicitly state which direction of the "
    "Spray Index means 'good' (per the injected thresholds) and call out at least one hour where you deferred to "
    "or OVERRODE it, with the reason (e.g. a high but pre-dawn/dark index, or a middling index at an "
    "above-threshold unsuitable-band midday hour). Cite real Delta-T/wind/index values; never invent. With no "
    "candidate windows: can_spray_tomorrow=False, populate limiting_factors, low confidence. "
    "Set target_date to the given date."
)

_COORD_STATIC = (
    "You are the coordinator. Consume the ALREADY-TYPED IrrigationAdvice and SprayAdvice plus deterministic "
    "conflict facts and produce the RECONCILIATION only: a grower-facing summary, conflicts_resolved, and "
    "overall_confidence (the two sub-advices are re-attached for you — do not echo them). Never recompute "
    "physics. Genuinely RECONCILE: sequence the day (e.g. 'spray the morning ideal-Delta-T window, irrigate "
    "after sunset') and populate conflicts_resolved from the conflict facts — if the plans are independent, "
    "say so explicitly rather than leaving it empty. The summary must not propose spraying when "
    "can_spray_tomorrow is False and must not contradict should_irrigate_tomorrow. Set overall_confidence as a "
    "lower-bounded reconciliation of the two sub-confidences — never above the most confident leg — dragged "
    "down by any unresolved interaction."
)


# --- agents ----------------------------------------------------------------------

# Model is bound at RUN time (model=config.MODEL), not construction — so importing this module
# needs no API key (the provider is only instantiated on a real run), and tests override it
# with TestModel (override wins in Agent._get_model, so config.MODEL is never inferred).
irrigation_agent = Agent(
    deps_type=IrrDeps, output_type=IrrigationAdvice,
    instructions=_IRR_STATIC, retries=2,  # TODO(B2): observability via capabilities=[Instrumentation(...)] + Logfire
)
spray_agent = Agent(
    deps_type=SprayDeps, output_type=SprayAdvice,
    instructions=_SPRAY_STATIC, retries=2,  # TODO(B2): observability via capabilities=[Instrumentation(...)] + Logfire
)
coordinator_agent = Agent(
    deps_type=CoordDeps, output_type=Reconciliation,  # sub-advices re-attached in code (no verbatim echo)
    instructions=_COORD_STATIC, retries=2,  # TODO(B2): observability via capabilities=[Instrumentation(...)] + Logfire
)


@irrigation_agent.instructions
def _irr_instructions(ctx: RunContext[IrrDeps]) -> str:
    return render_irrigation_context(ctx.deps)


@spray_agent.instructions
def _spray_instructions(ctx: RunContext[SprayDeps]) -> str:
    return render_spray_context(ctx.deps)


@coordinator_agent.instructions
def _coord_instructions(ctx: RunContext[CoordDeps]) -> str:
    return render_coordinator_context(ctx.deps)


@irrigation_agent.output_validator
def _validate_irrigation(ctx: RunContext[IrrDeps], out: IrrigationAdvice) -> IrrigationAdvice:
    f = ctx.deps.features
    if abs(out.current_depletion_mm - f.current_depletion_mm) > 0.5:
        raise ModelRetry(
            f"current_depletion_mm must equal the computed {f.current_depletion_mm} mm "
            f"(you returned {out.current_depletion_mm}); copy it verbatim, do not recompute."
        )
    cap = ctx.deps.crop.taw_mm
    if out.recommended_depth_mm is not None and out.recommended_depth_mm > cap + 0.5:
        raise ModelRetry(f"recommended_depth_mm {out.recommended_depth_mm} exceeds field-capacity cap {cap} mm")
    if out.target_date != ctx.deps.target_date:
        raise ModelRetry(f"target_date must be {ctx.deps.target_date.isoformat()}")
    return out


@spray_agent.output_validator
def _validate_spray(ctx: RunContext[SprayDeps], out: SprayAdvice) -> SprayAdvice:
    candidates = ctx.deps.features.windows
    for w in out.recommended_windows:
        contained = any(c.start <= w.start and w.end <= c.end for c in candidates)
        if not contained:
            raise ModelRetry(
                f"recommended window {w.start:%H:%M}-{w.end:%H:%M} is not within any candidate window — "
                "choose only from the provided candidates (trimming allowed, inventing not)."
            )
    if out.target_date != ctx.deps.target_date:
        raise ModelRetry(f"target_date must be {ctx.deps.target_date.isoformat()}")
    return out


@coordinator_agent.output_validator
def _validate_coordinator(ctx: RunContext[CoordDeps], out: Reconciliation) -> Reconciliation:
    # You can't be more confident overall than your most confident leg.
    legs = max(ctx.deps.irrigation.confidence, ctx.deps.spray.confidence)
    if out.overall_confidence > legs + 0.01:
        raise ModelRetry(
            f"overall_confidence {out.overall_confidence} cannot exceed the most confident leg ({legs}); "
            "reconcile down, do not inflate."
        )
    return out


# --- run helpers (called by the graph nodes; agents are module-level for override) ---

# NOTE: each run helper passes model=config.MODEL even though a test override(model=) wins —
# pydantic-ai 1.107 raises UserError under an override if NEITHER agent.model nor a run-time
# model is set, and the agents are built with no model so import needs no API key.

async def run_irrigation_agent(crop: Deps, features: IrrigationFeatures, dq: DataQuality, target_date: date, run_date: date) -> IrrigationAdvice:
    deps = IrrDeps(crop=crop, features=features, data_quality=dq, target_date=target_date, run_date=run_date)
    res = await irrigation_agent.run(render_irrigation_input(features, target_date), deps=deps, model=config.MODEL)
    out = res.output
    conf, caveat = _degrade(out.confidence, dq)
    return out.model_copy(update={
        "confidence": conf,
        "rationale": out.rationale + (f"\n[caveat — {caveat}]" if caveat else ""),
    })


async def run_spray_agent(crop: Deps, features: SprayFeatures, dq: DataQuality, target_date: date, run_date: date) -> SprayAdvice:
    deps = SprayDeps(crop=crop, features=features, data_quality=dq, target_date=target_date, run_date=run_date)
    res = await spray_agent.run(render_spray_input(features), deps=deps, model=config.MODEL)
    out = res.output
    conf, caveat = _degrade(out.confidence, dq)
    return out.model_copy(update={
        "confidence": conf,
        "limiting_factors": out.limiting_factors + ([caveat] if caveat else []),
    })


async def run_coordinator_agent(
    crop: Deps, irrigation: IrrigationAdvice, spray: SprayAdvice,
    conflict_facts: list[str], dq: DataQuality, target_date: date, run_date: date,
) -> DailyFarmAdvisory:
    deps = CoordDeps(
        crop=crop, irrigation=irrigation, spray=spray,
        conflict_facts=conflict_facts, data_quality=dq, target_date=target_date, run_date=run_date,
    )
    res = await coordinator_agent.run(render_coordinator_input(deps), deps=deps, model=config.MODEL)
    rec = res.output  # Reconciliation
    conf, caveat = _degrade(rec.overall_confidence, dq)
    # Enforce the invariant by construction: overall can't exceed the most confident leg.
    conf = round(min(conf, max(irrigation.confidence, spray.confidence)), 3)
    # Re-attach the sub-advices VERBATIM in code -> they cannot drift, and the LLM never echoes them.
    return DailyFarmAdvisory(
        date=target_date, irrigation=irrigation, spray=spray,
        summary=rec.summary + (f"\n[caveat — {caveat}]" if caveat else ""),
        conflicts_resolved=rec.conflicts_resolved + ([caveat] if caveat else []),
        overall_confidence=conf,
    )
