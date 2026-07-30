"""The pydantic-evals surface: the oracles, wrapped as Dataset evaluators.

The eval gate. `oracles.py` and `asymmetric.py` hold the *scoring
logic* -- pure functions over `features.py`. This module is the thin adapter that
exposes that logic to `pydantic_evals`: three `Evaluator` subclasses and a
`build_advisory_dataset` that assembles them into a `Dataset` scoring a
`DailyFarmAdvisory` against its own deterministic ground truth.

The wrappers hold NO scoring logic of their own -- each calls straight into
`oracles`/`asymmetric`. That is the point: there is one place the physics-vs-cost
scoring lives, and both consumers -- these evaluators and the DB-writing golden
driver in `run.py` -- go through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from vinea.contracts import DailyFarmAdvisory
from vinea.deps import Deps
from vinea.evals.asymmetric import IrrigationDecision, score_decisions
from vinea.evals.oracles import SprayRuleEvaluator, WaterBalanceOracle
from vinea.features import build_irrigation_features
from vinea.ingest import WeatherRow


@dataclass(frozen=True)
class AdvisoryInputs:
    """What a case is scored against: the exact inputs the graph ran on.

    Tuples (not lists) so the case is hashable and obviously frozen -- the golden
    inputs are held constant on purpose (golden.py).
    """

    history: tuple[WeatherRow, ...]
    forecast: tuple[WeatherRow, ...]
    deps: Deps
    run_date: date


# Metric names are the contract between an evaluator and the golden driver (run.py),
# which reads them off the report to write one eval_runs row each. Keep them stable.
DEPLETION_ERROR = "depletion_error_mm"
DEPLETION_OK = "depletion_within_tolerance"
INVALID_WINDOWS = "invalid_window_count"
WINDOWS_OK = "all_windows_valid"
ASYMMETRIC_COST = "asymmetric_cost"
IRRIGATION_CORRECT = "irrigation_decision_correct"
ORACLE_SHOULD = "oracle_should_irrigate"


@dataclass
class WaterBalanceEvaluator(Evaluator[AdvisoryInputs, DailyFarmAdvisory, None]):
    """Scores the echoed depletion against the real water balance (physics)."""

    tolerance_mm: float = 0.5

    def evaluate(self, ctx: EvaluatorContext[AdvisoryInputs, DailyFarmAdvisory, None]) -> dict:
        score = WaterBalanceOracle(tolerance_mm=self.tolerance_mm).score(
            list(ctx.inputs.history), list(ctx.inputs.forecast), ctx.inputs.deps,
            ctx.inputs.run_date, ctx.output.irrigation,
        )
        return {DEPLETION_ERROR: score.depletion_error_mm, DEPLETION_OK: score.within_tolerance}


@dataclass
class SprayWindowEvaluator(Evaluator[AdvisoryInputs, DailyFarmAdvisory, None]):
    """Scores each recommended window against the deterministic candidates."""

    def evaluate(self, ctx: EvaluatorContext[AdvisoryInputs, DailyFarmAdvisory, None]) -> dict:
        score = SprayRuleEvaluator().score(
            list(ctx.inputs.forecast), ctx.inputs.deps, ctx.inputs.run_date, ctx.output.spray
        )
        return {INVALID_WINDOWS: score.invalid_window_count, WINDOWS_OK: score.all_windows_valid}


@dataclass
class AsymmetricCostEvaluator(Evaluator[AdvisoryInputs, DailyFarmAdvisory, None]):
    """Scores the irrigate/hold call with the asymmetric cost (missed ~5x).

    The physics (did we get the depletion right) is the WaterBalanceEvaluator's job;
    this one scores the *decision* and how much getting it wrong costs a grower. The
    two are kept separate on purpose: the water balance is physics and doesn't
    know the price of a mistake.
    """

    def evaluate(self, ctx: EvaluatorContext[AdvisoryInputs, DailyFarmAdvisory, None]) -> dict:
        truth = build_irrigation_features(
            list(ctx.inputs.history), list(ctx.inputs.forecast), ctx.inputs.deps, ctx.inputs.run_date
        )
        should = truth.current_depletion_mm >= truth.raw_mm
        decision = IrrigationDecision(
            oracle_should_irrigate=should,
            model_recommends_irrigate=ctx.output.irrigation.should_irrigate_tomorrow,
        )
        score = score_decisions([decision])
        return {
            ASYMMETRIC_COST: score.weighted_cost,
            IRRIGATION_CORRECT: score.false_negatives == 0 and score.false_positives == 0,
            # Emitted so the golden driver can compute recall over the should-cases.
            ORACLE_SHOULD: should,
        }


def build_advisory_dataset(cases: list[Case], *, tolerance_mm: float = 0.5) -> Dataset:
    """Assemble the eval gate: a Dataset scoring advisories with all three oracles."""
    return Dataset(
        name="golden_advisory",
        cases=cases,
        evaluators=[
            WaterBalanceEvaluator(tolerance_mm=tolerance_mm),
            SprayWindowEvaluator(),
            AsymmetricCostEvaluator(),
        ],
    )
