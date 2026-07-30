"""Run a golden replay, score it, and write eval_runs tagged with the five drift tags.

Replay the golden fixtures through a task (the graph, or a fixture), score with the
deterministic oracles + the asymmetric evaluator via the pydantic-evals Dataset, and
persist one `eval_runs` row per evaluator -- each carrying the five drift tags
(golden.py) so a moved score is attributable.

The rows link to `prompt_version` (and the rest of the five tags), which is the
governance hook: staging→production promotion gates on an eval pass here plus
a human approver.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from statistics import fmean

from pydantic_evals import Case
from pydantic_evals.reporting import EvaluationReport
from sqlmodel import Session

from vinea.contracts import DailyFarmAdvisory
from vinea.db.models import EvalRun
from vinea.deps import Deps
from vinea.evals.asymmetric import RECALL_GATE, AsymmetricScore
from vinea.evals.dataset import (
    ASYMMETRIC_COST,
    DEPLETION_ERROR,
    DEPLETION_OK,
    INVALID_WINDOWS,
    IRRIGATION_CORRECT,
    ORACLE_SHOULD,
    WINDOWS_OK,
    AdvisoryInputs,
    build_advisory_dataset,
)
from vinea.evals.golden import DATASET_VERSION, GOLDEN_RUN_DATE, DriftTags, drift_tags, load_golden


@dataclass(frozen=True)
class EvalResult:
    evaluator: str
    score: float
    passed: bool
    recall_should_irrigate: float | None
    detail: dict


# The task the Dataset runs each case through: golden inputs -> a fresh advisory. The
# real one runs the graph (a live model, so behind ALLOW_MODEL_REQUESTS, not in CI); a
# test injects a fixture task, which is why the driver is offline-safe.
Task = Callable[[AdvisoryInputs], DailyFarmAdvisory | Awaitable[DailyFarmAdvisory]]


def write_eval_run(
    session: Session,
    result: EvalResult,
    tags: DriftTags,
    *,
    advisory_id: int | None = None,
) -> EvalRun:
    """Persist one evaluator's result as an eval_runs row. Does not commit.

    All five drift tags are NOT NULL on the row -- an untagged score isn't
    evidence -- so a moved score later is always attributable to whichever of the
    five moved with it.
    """
    row = EvalRun(
        prompt_version=tags.prompt_version,
        model_id=tags.model_id,
        deps_hash=tags.deps_hash,
        code_sha=tags.code_sha,
        dataset_version=tags.dataset_version,
        evaluator=result.evaluator,
        score=result.score,
        recall_should_irrigate=result.recall_should_irrigate,
        passed=result.passed,
        detail=result.detail,
        advisory_id=advisory_id,
    )
    session.add(row)
    session.flush()
    return row


def asymmetric_result(score: AsymmetricScore) -> EvalResult:
    """Adapt an AsymmetricScore into an EvalResult row-shape.

    The recall gate is the pass condition: a run passes only if recall on
    'should irrigate' clears the gate, regardless of how good the weighted cost looks.
    """
    return EvalResult(
        evaluator="asymmetric_cost",
        score=score.weighted_cost,
        passed=score.passed,
        recall_should_irrigate=score.recall_should_irrigate,
        detail={
            "false_negatives": score.false_negatives,
            "false_positives": score.false_positives,
            "weighted_cost": score.weighted_cost,
        },
    )


@dataclass(frozen=True)
class GoldenEvalOutcome:
    """The result of one golden replay: the raw report plus the persisted rows."""

    report: EvaluationReport
    results: list[EvalResult]
    rows: list[EvalRun]


def _metric(case, name):
    result = case.scores.get(name) or case.assertions.get(name)
    return None if result is None else result.value


def _results_from_report(report: EvaluationReport) -> list[EvalResult]:
    """Fold a Dataset report into one EvalResult per evaluator.

    The report is the single scoring path (dataset.py's evaluators wrap the oracles);
    this just aggregates it into row-shape. Over the one golden day the aggregation is
    trivial, but it's written for N cases so a larger golden set needs no change here.
    """
    cases = report.cases
    if not cases:
        raise ValueError("golden replay produced no scored cases -- did the task fail?")

    depletion_errors = [_metric(c, DEPLETION_ERROR) for c in cases]
    invalid_windows = [_metric(c, INVALID_WINDOWS) for c in cases]
    costs = [_metric(c, ASYMMETRIC_COST) for c in cases]

    # Recall over the should-irrigate cases only -- the gated number.
    should = [c for c in cases if _metric(c, ORACLE_SHOULD)]
    if should:
        caught = sum(1 for c in should if _metric(c, IRRIGATION_CORRECT))
        recall = caught / len(should)
    else:
        recall = 1.0  # nothing to miss -> vacuously perfect (see asymmetric.py)

    return [
        EvalResult(
            evaluator="water_balance",
            score=fmean(depletion_errors),
            passed=all(_metric(c, DEPLETION_OK) for c in cases),
            recall_should_irrigate=None,
            detail={"max_depletion_error_mm": max(depletion_errors), "cases": len(cases)},
        ),
        EvalResult(
            evaluator="spray_rule",
            score=float(sum(invalid_windows)),
            passed=all(_metric(c, WINDOWS_OK) for c in cases),
            recall_should_irrigate=None,
            detail={"invalid_window_count": sum(invalid_windows), "cases": len(cases)},
        ),
        EvalResult(
            evaluator="asymmetric_cost",
            score=float(sum(costs)),
            passed=recall >= RECALL_GATE,
            recall_should_irrigate=recall,
            detail={"weighted_cost": sum(costs), "should_cases": len(should)},
        ),
    ]


def run_golden_eval(
    session: Session,
    *,
    task: Task,
    deps: Deps,
    prompt_version: str,
    model_id: str,
    dataset_version: str = DATASET_VERSION,
    tolerance_mm: float = 0.5,
) -> GoldenEvalOutcome:
    """Replay the frozen golden fixtures through `task`, score, and persist.

    Holds the inputs constant (golden.py) and varies everything else, so a moved score
    is attributable to whichever of the five drift tags moved with it. One eval_runs
    row per evaluator, all five tags NOT NULL. Does not commit -- the caller owns the
    transaction boundary.
    """
    lr = load_golden()
    inputs = AdvisoryInputs(
        history=tuple(lr.history), forecast=tuple(lr.forecast), deps=deps, run_date=GOLDEN_RUN_DATE
    )
    dataset = build_advisory_dataset([Case(name="golden-day", inputs=inputs)], tolerance_mm=tolerance_mm)
    report = dataset.evaluate_sync(task, name="golden_replay", progress=False)

    tags = drift_tags(
        deps, prompt_version=prompt_version, model_id=model_id, dataset_version=dataset_version
    )
    results = _results_from_report(report)
    rows = [write_eval_run(session, r, tags) for r in results]
    return GoldenEvalOutcome(report=report, results=results, rows=rows)
