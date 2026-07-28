"""Typed Pydantic v2 contracts — the boundaries between the FeatureBuilder, the three
agents, and the graph. Pure `pydantic` (zero pydantic-ai / network imports) so they are
usable directly as `output_type=...` in phase 3 and unit-testable in isolation.

These models are the enforcement point for "structured outputs &
grounding" and "clean typed boundaries": `Field` constraints + validators are *runtime
guardrails* (an out-of-range confidence or a contradictory advice is rejected before it
reaches the grower), and a structured `evidence: list[GroundingFact]` stops the LLM from
inventing numbers — every claim must cite a real input.

Datetimes are tz-naive, site-local (consistent with ingest.py). TODO: a timezone policy
and a typed Enum for limiting_factors are deferred.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ingest import DataQuality

# --- shared enums --------------------------------------------------------------

Band = Literal["inversion_risk", "ideal", "marginal", "unsuitable"]
WindClass = Literal["near_calm", "ok", "drift"]


# --- grounding -----------------------------------------------------------------

class GroundingFact(BaseModel):
    """One cited input behind a rationale point (forces no-invented-numbers)."""

    model_config = ConfigDict(extra="forbid")

    factor: str = Field(description="what this fact is about, e.g. 'midday Delta T'")
    value: float | str = Field(description="the real input value")
    source_column: str | None = Field(default=None, description="originating CSV column, e.g. 'Delta T (°C)'")
    interpretation: str = Field(description="what it implies, e.g. 'above 8 °C marginal band'")


# --- agent output contracts ----------------------------------------------------

class SprayWindow(BaseModel):
    """A contiguous sprayable interval. `end` is exclusive (last suitable hour + 1h)."""

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    reason: str
    limiting_factors: list[str] = Field(default_factory=list, description="gates bounding this window")

    @model_validator(mode="after")
    def _end_after_start(self) -> SprayWindow:
        if self.end <= self.start:
            raise ValueError("SprayWindow.end must be after start")
        return self


class SprayAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_date: date
    can_spray_tomorrow: bool
    recommended_windows: list[SprayWindow] = Field(default_factory=list)
    limiting_factors: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, description="0..1 self-reported certainty")
    rationale: str
    evidence: list[GroundingFact] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> SprayAdvice:
        if self.can_spray_tomorrow and not self.recommended_windows:
            raise ValueError("can_spray_tomorrow=True requires at least one recommended window")
        if not self.can_spray_tomorrow and self.recommended_windows:
            raise ValueError("can_spray_tomorrow=False must not carry recommended_windows")
        if not self.can_spray_tomorrow and not self.limiting_factors:
            raise ValueError("can_spray_tomorrow=False requires limiting_factors explaining why")
        next_midnight = datetime.combine(self.target_date + timedelta(days=1), time(0, 0))
        for w in self.recommended_windows:
            # start must fall on target_date; end may roll to the following midnight (23:00 +1h).
            if w.start.date() != self.target_date or w.end > next_midnight:
                raise ValueError(f"window {w.start}–{w.end} is not within target_date {self.target_date}")
        return self


class IrrigationAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_date: date
    should_irrigate_tomorrow: bool
    recommended_depth_mm: float | None = Field(default=None, ge=0, description="refill depth (mm)")
    current_depletion_mm: float = Field(ge=0, description="root-zone depletion (mm)")
    confidence: float = Field(ge=0, le=1, description="0..1 self-reported certainty")
    rationale: str
    evidence: list[GroundingFact] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> IrrigationAdvice:
        if self.should_irrigate_tomorrow and not (self.recommended_depth_mm and self.recommended_depth_mm > 0):
            raise ValueError("should_irrigate_tomorrow=True requires recommended_depth_mm > 0")
        if not self.should_irrigate_tomorrow and self.recommended_depth_mm:
            raise ValueError("should_irrigate_tomorrow=False must not carry recommended_depth_mm")
        return self


class DailyFarmAdvisory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    irrigation: IrrigationAdvice
    spray: SprayAdvice
    summary: str = Field(description="grower-facing, plain language")
    conflicts_resolved: list[str] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0, le=1, description="reconciled 0..1 confidence")

    @model_validator(mode="after")
    def _dates_align(self) -> DailyFarmAdvisory:
        if self.irrigation.target_date != self.date or self.spray.target_date != self.date:
            raise ValueError("child advice target_date must equal advisory date")
        return self


class Reconciliation(BaseModel):
    """The Coordinator LLM's output — ONLY the reconciliation fields. The two sub-advices are
    re-attached VERBATIM in code (agents.run_coordinator_agent), so the LLM never has to echo
    two nested objects and they cannot drift."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="grower-facing, plain language, sequenced day")
    conflicts_resolved: list[str] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0, le=1, description="reconciled 0..1 confidence")


# --- deterministic feature types (filled by the FeatureBuilder; read by agents) -

class HourClassification(BaseModel):
    """Per-hour spray feature record (raw inputs + each gate result)."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    delta_t_c: float | None = Field(default=None, description="wet-bulb depression (°C)")
    wind_ms: float | None = Field(default=None, description="wind speed (m/s)")
    spray_index: float | None = Field(default=None, description="provided suitability score (0-100)")
    precip_mm: float | None = Field(default=None, description="precipitation (mm/h)")
    vpd_kpa: float | None = Field(default=None, description="vapour-pressure deficit (kPa)")
    band: Band | None = None
    wind_class: WindClass | None = None
    rain_fast_ok: bool = True
    index_ok: bool = True
    suitable: bool = False
    gate_failures: list[str] = Field(default_factory=list)


class IrrigationFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # assumptions echoed for grounding
    crop: str
    kc: float = Field(description="crop coefficient (dimensionless)")
    taw_mm: float = Field(description="total available water (mm)")
    raw_mm: float = Field(description="readily available water / MAD trigger (mm)")
    mad_fraction: float = Field(description="management-allowed depletion fraction p")
    initial_depletion_mm: float = Field(description="t=0 depletion assumption (mm)")
    effective_rain_fraction: float = Field(description="fraction of gross rain that is effective")
    rain_skip_mm: float = Field(description="skip irrigation if effective forecast rain >= this (mm)")
    refill_fraction: float = Field(description="refill target fraction (1.0=FC; <1 deficit)")
    # computed numbers
    cumulative_etc_mm: float = Field(description="sum of ETc over history, unclamped demand (mm)")
    current_depletion_mm: float = Field(description="running balance, clamped to [0, TAW] (mm)")
    etc_tomorrow_mm: float = Field(description="tomorrow's crop water demand ETc (mm)")
    forecast_rain_tomorrow_mm: float = Field(description="gross forecast rain tomorrow (mm)")
    effective_rain_tomorrow_mm: float = Field(description="effective forecast rain tomorrow (mm)")
    projected_depletion_mm: float = Field(description="depletion after tomorrow, clamped (mm)")
    should_irrigate_trigger: bool = Field(description="mechanical RAW/MAD trigger (LLM may override)")
    recommended_depth_mm: float | None = Field(default=None, description="refill depth toward FC (mm)")
    skipped_et0_hours: int = 0
    notes: list[str] = Field(default_factory=list)


class SprayFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_date: date
    can_spray: bool
    windows: list[SprayWindow] = Field(default_factory=list)
    hours: list[HourClassification] = Field(default_factory=list)
    limiting_factors: list[str] = Field(default_factory=list)
    band_counts: dict[str, int] = Field(default_factory=dict)


class FarmFeatures(BaseModel):
    """The deterministic numbers the agents reason over — the LLM/deterministic boundary
    made concrete. Agents read these clean numbers; they never do arithmetic in-context."""

    model_config = ConfigDict(extra="forbid")

    as_of: date
    target_date: date
    data_quality: DataQuality
    irrigation: IrrigationFeatures
    spray: SprayFeatures
