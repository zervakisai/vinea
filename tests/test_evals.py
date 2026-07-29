"""S7.4 / S7.5 / S7.6 -- the eval gate: oracles, asymmetric cost, drift tags, judge.

The oracle/asymmetric/golden pieces are pure or DB-backed and fully offline. The
judge is exercised with a FunctionModel, so no live model is needed to prove the
confinement (judge sees the summary, not the numbers).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals import Case

from tests.conftest import open_ops_session
from vinea.contracts import DailyFarmAdvisory, IrrigationAdvice, SprayAdvice, SprayWindow
from vinea.deps import WINE_GRAPES, Deps
from vinea.evals import asymmetric, golden, judge
from vinea.evals.dataset import AdvisoryInputs, build_advisory_dataset
from vinea.evals.oracles import SprayRuleEvaluator, WaterBalanceOracle
from vinea.evals.run import asymmetric_result, run_golden_eval, write_eval_run
from vinea.features import spray_features_for_tomorrow

RUN_DATE = golden.GOLDEN_RUN_DATE
TARGET = RUN_DATE + timedelta(days=1)


# --- S7.4 / B2-2: oracles wrap the SAME code, they don't reimplement ---------


def test_water_balance_oracle_agrees_with_production_on_a_correct_output():
    lr = golden.load_golden()
    score = WaterBalanceOracle().score(
        list(lr.history), list(lr.forecast), WINE_GRAPES, RUN_DATE, _irrigation(depletion=133.5)
    )
    # 197.11 mm ET0 x Kc 0.70 = 137.98 mm ETc, less 4.48 mm effective rain -> 133.5 mm.
    assert score.oracle_depletion_mm == pytest.approx(133.5, abs=0.5)
    assert score.within_tolerance  # a model echoing 150 is spot on


def test_water_balance_oracle_flags_a_wrong_depletion():
    lr = golden.load_golden()
    score = WaterBalanceOracle(tolerance_mm=0.5).score(
        list(lr.history), list(lr.forecast), WINE_GRAPES, RUN_DATE, _irrigation(depletion=100.0)
    )
    assert not score.within_tolerance
    assert score.depletion_error_mm == pytest.approx(33.5, abs=0.5)


def test_spray_oracle_accepts_a_real_candidate_and_rejects_an_invented_one():
    lr = golden.load_golden()
    candidates = spray_features_for_tomorrow(list(lr.forecast), WINE_GRAPES, RUN_DATE).windows
    assert candidates, "fixture should yield at least one candidate window"
    real = candidates[0]
    # A valid SprayWindow (start < end, on target_date) that is NOT a candidate:
    # 03:00 is dark, so the spray gates never produce a window there.
    invented = SprayWindow(
        start=datetime(TARGET.year, TARGET.month, TARGET.day, 3, 0),
        end=datetime(TARGET.year, TARGET.month, TARGET.day, 4, 0),
        reason="invented (pre-dawn, clears no gate)",
    )

    ok = SprayRuleEvaluator().score(list(lr.forecast), WINE_GRAPES, RUN_DATE, _spray([real]))
    assert ok.all_windows_valid

    bad = SprayRuleEvaluator().score(list(lr.forecast), WINE_GRAPES, RUN_DATE, _spray([invented]))
    assert not bad.all_windows_valid
    assert bad.invalid_window_count == 1


# --- S7.4 / B2-3: the asymmetric evaluator + recall gate ---------------------


def test_missed_irrigation_costs_5x_an_unnecessary_one():
    missed = asymmetric.score_decisions(
        [asymmetric.IrrigationDecision(oracle_should_irrigate=True, model_recommends_irrigate=False)]
    )
    unnecessary = asymmetric.score_decisions(
        [asymmetric.IrrigationDecision(oracle_should_irrigate=False, model_recommends_irrigate=True)]
    )
    assert missed.weighted_cost == pytest.approx(5.0)
    assert unnecessary.weighted_cost == pytest.approx(1.0)


def test_recall_gate_fails_a_model_that_misses_irrigations_even_if_accurate_overall():
    decisions = [
        asymmetric.IrrigationDecision(oracle_should_irrigate=True, model_recommends_irrigate=(i >= 2))
        for i in range(10)
    ]
    score = asymmetric.score_decisions(decisions)
    assert score.recall_should_irrigate == pytest.approx(0.8)
    assert not score.passed


def test_recall_gate_passes_when_all_irrigations_are_caught():
    decisions = [
        asymmetric.IrrigationDecision(oracle_should_irrigate=True, model_recommends_irrigate=True)
        for _ in range(5)
    ] + [
        asymmetric.IrrigationDecision(oracle_should_irrigate=False, model_recommends_irrigate=True)
    ]
    score = asymmetric.score_decisions(decisions)
    assert score.recall_should_irrigate == pytest.approx(1.0)
    assert score.passed


# --- S7.5: golden replay + the five drift tags -> eval_runs ------------------


def test_drift_tags_capture_the_five_things_that_move_a_score():
    tags = golden.drift_tags(WINE_GRAPES, prompt_version="7", model_id="openai:gpt-4o-mini")
    assert tags.prompt_version == "7"
    assert tags.model_id == "openai:gpt-4o-mini"
    assert len(tags.deps_hash) == 16
    assert tags.dataset_version == golden.DATASET_VERSION
    # The oracle-change case B2 singles out: change a constant, the deps_hash moves.
    other = golden.drift_tags(
        Deps(effective_rain_fraction=0.75), prompt_version="7", model_id="openai:gpt-4o-mini"
    )
    assert other.deps_hash != tags.deps_hash


def test_eval_run_is_written_with_all_five_tags(committing_db):
    from sqlmodel import select

    from vinea.db.models import EvalRun

    tags = golden.drift_tags(WINE_GRAPES, prompt_version="7", model_id="openai:gpt-4o-mini")
    score = asymmetric.score_decisions(
        [asymmetric.IrrigationDecision(oracle_should_irrigate=True, model_recommends_irrigate=True)]
    )
    with open_ops_session(committing_db) as s:
        write_eval_run(s, asymmetric_result(score), tags)
        s.commit()

    with open_ops_session(committing_db) as s:
        row = s.exec(select(EvalRun).where(EvalRun.evaluator == "asymmetric_cost")).one()
        assert row.prompt_version == "7"
        assert row.deps_hash and row.code_sha and row.dataset_version
        assert row.recall_should_irrigate == pytest.approx(1.0)


# --- S7.4 (pydantic-evals surface): the oracles wrapped as Dataset evaluators -


def _golden_inputs() -> AdvisoryInputs:
    lr = golden.load_golden()
    return AdvisoryInputs(
        history=tuple(lr.history), forecast=tuple(lr.forecast), deps=WINE_GRAPES, run_date=RUN_DATE
    )


def test_dataset_scores_a_correct_advisory_through_pydantic_evals():
    ds = build_advisory_dataset([Case(name="golden", inputs=_golden_inputs())])
    report = ds.evaluate_sync(lambda _i: _advisory_with(depletion=133.5, recommend=True), progress=False)

    case = report.cases[0]
    assert case.scores["depletion_error_mm"].value == pytest.approx(0.0, abs=0.5)
    assert case.assertions["depletion_within_tolerance"].value is True
    assert case.assertions["all_windows_valid"].value is True
    assert case.scores["asymmetric_cost"].value == pytest.approx(0.0)
    assert case.assertions["irrigation_decision_correct"].value is True


def test_dataset_flags_a_wrong_depletion_and_a_missed_irrigation():
    ds = build_advisory_dataset([Case(name="golden", inputs=_golden_inputs())])
    report = ds.evaluate_sync(lambda _i: _advisory_with(depletion=100.0, recommend=False), progress=False)

    case = report.cases[0]
    assert case.assertions["depletion_within_tolerance"].value is False
    assert case.scores["depletion_error_mm"].value == pytest.approx(33.5, abs=0.5)
    assert case.scores["asymmetric_cost"].value == pytest.approx(5.0)  # missed irrigation
    assert case.assertions["irrigation_decision_correct"].value is False


# --- S7.5 (golden driver): the replay writes one eval_runs row per evaluator --


def test_golden_replay_writes_a_passing_run_per_evaluator(committing_db):
    from sqlmodel import select

    from vinea.db.models import EvalRun

    with open_ops_session(committing_db) as s:
        outcome = run_golden_eval(
            s,
            task=lambda _i: _advisory_with(depletion=133.5, recommend=True),
            deps=WINE_GRAPES,
            prompt_version="7",
            model_id="openai:gpt-4o-mini",
        )
        s.commit()
        assert {r.evaluator for r in outcome.results} == {"water_balance", "spray_rule", "asymmetric_cost"}
        assert all(r.passed for r in outcome.results)

    with open_ops_session(committing_db) as s:
        rows = s.exec(select(EvalRun)).all()
        assert len(rows) == 3
        assert all(r.prompt_version == "7" and r.deps_hash and r.code_sha for r in rows)
        asym = next(r for r in rows if r.evaluator == "asymmetric_cost")
        assert asym.recall_should_irrigate == pytest.approx(1.0)


def test_golden_replay_fails_the_gate_on_a_missed_irrigation(committing_db):

    with open_ops_session(committing_db) as s:
        outcome = run_golden_eval(
            s,
            task=lambda _i: _advisory_with(depletion=100.0, recommend=False),
            deps=WINE_GRAPES,
            prompt_version="7",
            model_id="openai:gpt-4o-mini",
        )
        s.commit()

    by = {r.evaluator: r for r in outcome.results}
    assert by["water_balance"].passed is False  # 50mm off
    assert by["asymmetric_cost"].passed is False  # recall 0 on the one should-case
    assert by["asymmetric_cost"].recall_should_irrigate == pytest.approx(0.0)


# --- S7.6: LLM-as-judge, confined to the summary -----------------------------


def test_judge_sees_the_summary_not_the_numbers():
    captured = {}

    def judge_fn(messages, info: AgentInfo) -> ModelResponse:
        captured["prompt"] = messages[-1].parts[-1].content
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.output_tools[0].name,
                    args={
                        "is_clear": True, "is_grounded": True,
                        "names_no_invented_interaction": True, "rationale": "clear plan",
                    },
                    tool_call_id="c",
                )
            ]
        )

    import asyncio

    advisory = _advisory(plan="Irrigate at dawn; spray in the morning window; they don't interact.")
    with judge.judge_agent.override(model=FunctionModel(judge_fn)):
        verdict = asyncio.run(judge.judge_summary(advisory, model="openai:gpt-4o"))

    assert verdict.is_clear
    # The confinement: the judge got the plan text, and NOT the depletion number.
    assert "Irrigate at dawn" in captured["prompt"]
    assert "150" not in captured["prompt"]  # no numbers handed to the judge


# --- helpers ----------------------------------------------------------------


def _irrigation(*, depletion: float) -> IrrigationAdvice:
    return IrrigationAdvice(
        target_date=TARGET,
        should_irrigate_tomorrow=True,
        recommended_depth_mm=max(depletion, 1.0),
        current_depletion_mm=depletion,
        confidence=0.7,
        rationale="t",
    )


def _spray(windows: list[SprayWindow]) -> SprayAdvice:
    can = bool(windows)
    return SprayAdvice(
        target_date=TARGET,
        can_spray_tomorrow=can,
        recommended_windows=windows if can else [],
        limiting_factors=[] if can else ["none"],
        confidence=0.6,
        rationale="t",
    )


def _advisory_with(*, depletion: float, recommend: bool) -> DailyFarmAdvisory:
    irr = IrrigationAdvice(
        target_date=TARGET,
        should_irrigate_tomorrow=recommend,
        recommended_depth_mm=round(depletion, 1) if recommend else None,
        current_depletion_mm=depletion,
        confidence=0.7,
        rationale="t",
    )
    return DailyFarmAdvisory(
        date=TARGET,
        irrigation=irr,
        spray=_spray([]),
        summary="p",
        conflicts_resolved=[],
        overall_confidence=0.6,
    )


def _advisory(*, plan: str) -> DailyFarmAdvisory:
    return DailyFarmAdvisory(
        date=date(2025, 2, 9),
        irrigation=IrrigationAdvice(
            target_date=date(2025, 2, 9),
            should_irrigate_tomorrow=True,
            recommended_depth_mm=150.0,
            current_depletion_mm=150.0,
            confidence=0.7,
            rationale="t",
        ),
        spray=SprayAdvice(
            target_date=date(2025, 2, 9),
            can_spray_tomorrow=False,
            recommended_windows=[],
            limiting_factors=["none"],
            confidence=0.6,
            rationale="t",
        ),
        summary=plan,
        conflicts_resolved=[],
        overall_confidence=0.6,
    )
