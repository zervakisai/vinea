"""Contracts <-> rows. The only module allowed to know both shapes.

There is exactly one mapping layer, and this is it. The alternative -- each call
site doing its own `SQLModel(**advice.model_dump())` -- is how a schema forks:
contracts.py says one thing under `extra="forbid"`, the tables say another, and
the two drift silently because nothing compares them. Every conversion funnels
through the functions below, so a change to a contract breaks *here*, loudly, in
one place, instead of three modules downstream in a mismatched confidence number.

The naive-datetime rule, in one place because it is exactly the kind of
assumption that rots when scattered: `WeatherRow.timestamp` is naive (the CSVs
carry no offset, and the spray gates reason in local clock hours -- "06:00 is
morning" is a fact about the sun, not about UTC). The database columns are
`timestamptz`, because an instant is the right thing to store once more than one
region exists. This module is the seam: naive values are interpreted as UTC on
the way in and stripped back to naive on the way out, so a round-trip is the
identity. The day a source hands us a real offset, this is the one file that
changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime

from vinea.contracts import (
    DailyFarmAdvisory,
    IrrigationAdvice,
    Reconciliation,
    SprayAdvice,
)
from vinea.db.models import Advisory, GrowerConfig, WeatherObservation
from vinea.deps import Deps
from vinea.ingest import WeatherRow

# ---------------------------------------------------------------------------
# datetime boundary
# ---------------------------------------------------------------------------


def _to_db(value: datetime) -> datetime:
    """Naive -> aware UTC. Already-aware values pass through untouched."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _from_db(value: datetime) -> datetime:
    """Aware UTC -> naive, restoring exactly what the contract carried in."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


# ---------------------------------------------------------------------------
# Deps <-> grower_config
# ---------------------------------------------------------------------------


def deps_hash(deps: Deps) -> str:
    """A stable fingerprint of a Deps, for provenance.

    This is one of DESIGN.md B2's five drift tags. It answers "were these the
    thresholds in play?" a year later, which matters most for the case B2 calls
    out specifically: an intentional constant change (say `effective_rain_fraction`)
    should visibly move eval scores and be traceable to *that*, not mistaken for
    the model drifting.

    `sort_keys` and a fixed separator make this stable across processes and
    Python versions; `default=list` normalises Deps' tuples, since a JSON
    round-trip would turn them into lists anyway and the hash must not depend on
    which side of a serialisation boundary it was computed.
    """
    payload = json.dumps(asdict(deps), sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def deps_to_row(deps: Deps, *, tenant: str, location: str, region: str) -> GrowerConfig:
    """Render a Deps as a grower_config row.

    Note what isn't written: `raw_mm`. It's `mad_fraction * taw_mm`, computed in
    IrrigationFeatures, so persisting it would give the irrigation trigger a
    second home and a chance to disagree with itself.
    """
    return GrowerConfig(
        tenant=tenant,
        location=location,
        region=region,
        crop=deps.crop,
        irrigation_method=deps.irrigation_method,
        spray_sensitivity=deps.spray_sensitivity,
        kc=deps.kc,
        root_depth_m=deps.root_depth_m,
        taw_mm=deps.taw_mm,
        mad_fraction=deps.mad_fraction,
        initial_depletion_mm=deps.initial_depletion_mm,
        effective_rain_fraction=deps.effective_rain_fraction,
        rain_skip_mm=deps.rain_skip_mm,
        refill_fraction=deps.refill_fraction,
        deltat_ideal_low=deps.deltat_ideal[0],
        deltat_ideal_high=deps.deltat_ideal[1],
        deltat_inversion_below=deps.deltat_inversion_below,
        deltat_marginal_upper=deps.deltat_marginal_upper,
        wind_ideal_low_ms=deps.wind_ideal_ms[0],
        wind_ideal_high_ms=deps.wind_ideal_ms[1],
        spray_index_cutoff=deps.spray_index_cutoff,
        spray_index_higher_is_better=deps.spray_index_higher_is_better,
        rain_fast_hours=deps.rain_fast_hours,
        deps_hash=deps_hash(deps),
    )


def row_to_deps(row: GrowerConfig) -> Deps:
    """Rebuild the frozen Deps from its row. Tuples reassembled here."""
    return Deps(
        crop=row.crop,
        irrigation_method=row.irrigation_method,
        spray_sensitivity=row.spray_sensitivity,
        kc=row.kc,
        root_depth_m=row.root_depth_m,
        taw_mm=row.taw_mm,
        mad_fraction=row.mad_fraction,
        initial_depletion_mm=row.initial_depletion_mm,
        effective_rain_fraction=row.effective_rain_fraction,
        rain_skip_mm=row.rain_skip_mm,
        refill_fraction=row.refill_fraction,
        deltat_ideal=(row.deltat_ideal_low, row.deltat_ideal_high),
        deltat_inversion_below=row.deltat_inversion_below,
        deltat_marginal_upper=row.deltat_marginal_upper,
        wind_ideal_ms=(row.wind_ideal_low_ms, row.wind_ideal_high_ms),
        spray_index_cutoff=row.spray_index_cutoff,
        spray_index_higher_is_better=row.spray_index_higher_is_better,
        rain_fast_hours=row.rain_fast_hours,
    )


# ---------------------------------------------------------------------------
# WeatherRow <-> weather_observations
# ---------------------------------------------------------------------------


def weather_row_to_observation(
    row: WeatherRow, *, tenant: str, location: str, kind: str, source: str
) -> WeatherObservation:
    """Render one WeatherRow as an observation row.

    `kind` and `source` are supplied by the caller rather than inferred: the row
    itself has no idea whether it describes a forecast or a measurement, and that
    distinction is part of the natural key.
    """
    return WeatherObservation(
        tenant=tenant,
        location=location,
        observed_at=_to_db(row.timestamp),
        kind=kind,
        source=source,
        temp_c=row.temp_c,
        humidity_pct=row.humidity_pct,
        wind_ms=row.wind_ms,
        precip_mm=row.precip_mm,
        spray_index=row.spray_index,
        et0_mm=row.et0_mm,
        dew_point_c=row.dew_point_c,
        vpd_kpa=row.vpd_kpa,
        delta_t_c=row.delta_t_c,
        wind_dir_deg=row.wind_dir_deg,
        ghi_wm2=row.ghi_wm2,
    )


def observation_to_weather_row(obs: WeatherObservation) -> WeatherRow:
    """Rebuild a WeatherRow from its row.

    This is the direction that matters for S2: it is what lets the pipeline read
    from Postgres instead of a CSV without anything downstream noticing. A None
    column stays None -- the database preserves "we didn't observe this", it
    doesn't launder it into a zero.
    """
    return WeatherRow(
        timestamp=_from_db(obs.observed_at),
        temp_c=obs.temp_c,
        humidity_pct=obs.humidity_pct,
        wind_ms=obs.wind_ms,
        precip_mm=obs.precip_mm,
        spray_index=obs.spray_index,
        et0_mm=obs.et0_mm,
        dew_point_c=obs.dew_point_c,
        vpd_kpa=obs.vpd_kpa,
        delta_t_c=obs.delta_t_c,
        wind_dir_deg=obs.wind_dir_deg,
        ghi_wm2=obs.ghi_wm2,
    )


# ---------------------------------------------------------------------------
# DailyFarmAdvisory <-> advisories
# ---------------------------------------------------------------------------


def advisory_to_row(
    advisory: DailyFarmAdvisory,
    *,
    tenant: str,
    run_date: date,
    deps: Deps,
    model_id: str | None = None,
    prompt_name: str | None = None,
    prompt_version: str | None = None,
    prompt_source: str | None = None,
    code_sha: str | None = None,
    dataset_version: str | None = None,
    trace_id: str | None = None,
    degraded: bool = False,
    pre_correction_output: dict | None = None,
) -> Advisory:
    """Render a DailyFarmAdvisory + its provenance as a row.

    `mode="json"` on the dumps is load-bearing: it turns datetimes into ISO
    strings that JSONB can hold, and it is what makes `row_to_advisory` able to
    hand the dict straight back to pydantic for revalidation. The coordinator's
    fields (summary, conflicts_resolved, overall_confidence) sit flat on the
    advisory but are stored through a `Reconciliation`-shaped dict so the leg
    round-trips as its own validated contract.
    """
    reconciliation = Reconciliation(
        summary=advisory.summary,
        conflicts_resolved=advisory.conflicts_resolved,
        overall_confidence=advisory.overall_confidence,
    )
    return Advisory(
        tenant=tenant,
        run_date=run_date,
        target_date=advisory.date,
        irrigation=advisory.irrigation.model_dump(mode="json"),
        spray=advisory.spray.model_dump(mode="json"),
        reconciliation=reconciliation.model_dump(mode="json"),
        # Promoted for aggregation; written here, never read back as truth.
        irrigation_confidence=advisory.irrigation.confidence,
        spray_confidence=advisory.spray.confidence,
        overall_confidence=advisory.overall_confidence,
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        prompt_source=prompt_source,
        model_id=model_id,
        deps_hash=deps_hash(deps),
        code_sha=code_sha,
        dataset_version=dataset_version,
        trace_id=trace_id,
        degraded=degraded,
        pre_correction_output=pre_correction_output,
    )


def row_to_advisory(row: Advisory) -> DailyFarmAdvisory:
    """Rebuild the DailyFarmAdvisory from its row.

    The JSONB goes back through the contracts' own validators rather than being
    trusted: a row written by an older schema, or hand-edited in psql, fails here
    at `extra="forbid"` instead of reaching a grower as a half-valid advisory.
    The database is storage, not an authority on shape.
    """
    reconciliation = Reconciliation.model_validate(row.reconciliation)
    return DailyFarmAdvisory(
        date=row.target_date,
        irrigation=IrrigationAdvice.model_validate(row.irrigation),
        spray=SprayAdvice.model_validate(row.spray),
        summary=reconciliation.summary,
        conflicts_resolved=reconciliation.conflicts_resolved,
        overall_confidence=reconciliation.overall_confidence,
    )
