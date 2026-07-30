"""LLM-as-judge, confined to the one subjective artifact and the trajectory.

LLM-as-judge still has a job here, just a narrower one than "grade the
output". The grower-facing *summary* (the reconciliation's sequenced plan) is the
one genuinely subjective artifact in the pipeline -- there's no correct-numerical
answer to check it against -- and that's exactly what a judge model is for. It must
NEVER be pointed at a number that already has a correct answer (that's the oracle's
job); a judge on the depletion figure would be replacing physics with an opinion.

Two guardrails on the judge:

  1. The rubric names what it's checking (clear? grounded in the facts? no invented
     interaction?), because a judge is a model with its own biases (position,
     verbosity, self-enhancement, leniency) and "is this good?" invites all of them.
     A different, typically stronger model than the one being judged does the judging.

  2. Trajectory, not just the final answer. A broken Spray node whose error the
     Coordinator happens to paper over in the summary is a "lucky reconcile" -- the
     final plan reads fine, but a node was wrong. Scoring each leg's own output
     *before* reconciliation (the oracles do this) catches it; the judge scores only
     the summary, so the two together cover trajectory + outcome.

`judge_summary` takes a judge model and returns a typed verdict. Tests drive it with
a FunctionModel, so no live model is needed to exercise the confinement (that the
judge sees the summary, not the numbers).
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from vinea.contracts import DailyFarmAdvisory


class SummaryVerdict(BaseModel):
    """A judge's typed verdict on the grower-facing summary. Rubric-driven."""

    is_clear: bool = Field(description="Is the plan clear and actionable for a grower?")
    is_grounded: bool = Field(description="Does it stick to the facts it was given, inventing nothing?")
    names_no_invented_interaction: bool = Field(
        description="Does it avoid asserting an interaction not in the conflict facts?"
    )
    rationale: str


JUDGE_RUBRIC = (
    "You are grading the CLARITY of a vineyard day-plan summary written for a grower. "
    "You are NOT checking any number -- the numbers were verified deterministically elsewhere, "
    "and second-guessing them is not your job. Grade only: (1) is the plan clear and actionable, "
    "(2) is it grounded in the plan it was given rather than inventing steps, (3) does it avoid "
    "asserting an interaction between irrigation and spray that wasn't in the facts. Answer each "
    "as a boolean with a one-line rationale."
)


# The judge agent, constructed WITHOUT a bound model -- the model is passed at run()
# time, exactly like the core's agents. This keeps construction free of credentials and
# lets a test `.override()` it with a FunctionModel. The model used at run should be a
# DIFFERENT, typically stronger model than the one that produced the summary.
judge_agent = Agent(output_type=SummaryVerdict, instructions=JUDGE_RUBRIC)


async def judge_summary(advisory: DailyFarmAdvisory, *, model: str) -> SummaryVerdict:
    """Judge ONLY the summary -- never the numbers.

    The prompt deliberately carries the `summary` text and not the depletion/window
    figures: the judge's job is clarity of prose, and handing it the numbers would
    invite it to grade what the oracle already owns.
    """
    result = await judge_agent.run(
        f"Grade this grower day-plan summary for clarity:\n\n{advisory.summary}",
        model=model,
    )
    return result.output
