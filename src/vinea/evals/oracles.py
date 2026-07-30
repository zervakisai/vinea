"""Deterministic oracles: the SAME functions features.py has, wrapped for scoring.

The scoring half of observability shouldn't be an LLM judging an LLM,
not for the legs that have a correct numerical answer. So these oracles call
`features.build_irrigation_features` and `features.spray_features_for_tomorrow`
directly -- the exact code the deterministic core runs -- and compare the model's
output against their result. Not a reimplementation, not an approximation: a
*wrapper* of the real thing.

Why that matters beyond DRY: if the oracle reimplemented the water balance, a bug in
the reimplementation would make a correct model look wrong (or vice versa), and
you'd be scoring the model against a second, subtly-different physics. Wrapping the
real function means the oracle and production agree by construction.

The two roles this code plays are kept separate on purpose -- the circularity trap:
the output_validator in agents.py uses the same *facts* inline to force a retry;
these evaluators use them async, over historical runs, to score -- and they score
the pre-correction attempt, not the shipped output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from vinea.contracts import IrrigationAdvice, SprayAdvice
from vinea.deps import Deps
from vinea.features import build_irrigation_features, spray_features_for_tomorrow
from vinea.ingest import WeatherRow


@dataclass(frozen=True)
class WaterBalanceScore:
    depletion_error_mm: float  # |model - oracle|
    within_tolerance: bool
    oracle_depletion_mm: float
    claimed_depletion_mm: float


class WaterBalanceOracle:
    """Scores an irrigation output's depletion against the real water balance.

    Runs `features.build_irrigation_features` over the same inputs and compares the
    model's echoed `current_depletion_mm` to it. This is the physics; it does not
    know or care how much a mistake costs a grower -- that asymmetry lives in the
    asymmetric evaluator, on purpose.
    """

    def __init__(self, tolerance_mm: float = 0.5) -> None:
        self.tolerance_mm = tolerance_mm

    def score(
        self,
        history: list[WeatherRow],
        forecast: list[WeatherRow],
        deps: Deps,
        run_date: date,
        output: IrrigationAdvice,
    ) -> WaterBalanceScore:
        truth = build_irrigation_features(history, forecast, deps, run_date)
        error = abs(output.current_depletion_mm - truth.current_depletion_mm)
        return WaterBalanceScore(
            depletion_error_mm=error,
            within_tolerance=error <= self.tolerance_mm,
            oracle_depletion_mm=truth.current_depletion_mm,
            claimed_depletion_mm=output.current_depletion_mm,
        )


@dataclass(frozen=True)
class SprayRuleScore:
    all_windows_valid: bool  # every recommended window is a real candidate
    invalid_window_count: int
    recommended_count: int
    candidate_count: int


class SprayRuleEvaluator:
    """Scores a spray output against the deterministic candidate windows.

    Recomputes the candidates with `features.spray_features_for_tomorrow` and checks
    every recommended window is one of them -- a window the model invented (that
    clears no gate) is the failure this catches. Membership is by value equality
    (SprayWindow is a pydantic model), so no hashing is required.
    """

    def score(
        self,
        forecast: list[WeatherRow],
        deps: Deps,
        run_date: date,
        output: SprayAdvice,
    ) -> SprayRuleScore:
        candidates = spray_features_for_tomorrow(forecast, deps, run_date).windows
        invalid = [w for w in output.recommended_windows if w not in candidates]
        return SprayRuleScore(
            all_windows_valid=not invalid,
            invalid_window_count=len(invalid),
            recommended_count=len(output.recommended_windows),
            candidate_count=len(candidates),
        )
