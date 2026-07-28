"""The asymmetric-cost evaluator: missing an irrigation costs ~5x a false one.

DESIGN.md B2: missing an irrigation matters more than an unnecessary one -- a
vine that goes dry loses the season; a vine watered a day early loses nothing.
So the cost of the two error directions is asymmetric, roughly 5x, and that
asymmetry lives HERE, as a custom evaluator, **not** inside `features.py`. The
water balance is physics, and physics doesn't know how much a mistake costs a
grower. Baking a 5x fudge into the depletion calculation would corrupt the
number every other consumer trusts; keeping it in the evaluator means the
physics stays honest and the *business cost* is scored separately.

Two errors, weighted:

  - FALSE NEGATIVE (missed irrigation): the oracle says irrigate (depletion >=
    RAW) and the model said don't. Weighted 5x. This is the expensive
    direction.
  - FALSE POSITIVE (unnecessary irrigation): the oracle says hold and the
    model said irrigate. Weighted 1x. The acceptable failure direction.

And the number to *gate promotion on* is recall on "should have irrigated" --
kept near 100%. False positives are the acceptable failure by design, so
precision can slip; recall cannot. A prompt/model that improves overall
accuracy by trading away recall must NOT pass, which a symmetric score would
happily allow. Recall-gating is what encodes "never let a vine go dry to look
better on average".
"""

from __future__ import annotations

from dataclasses import dataclass

MISSED_IRRIGATION_WEIGHT = 5.0
UNNECESSARY_IRRIGATION_WEIGHT = 1.0
# Promotion requires recall on "should irrigate" at or above this. High on
# purpose: the whole point is that the expensive error stays rare.
RECALL_GATE = 0.95


@dataclass(frozen=True)
class IrrigationDecision:
    """One day's ground-truth-vs-model irrigation call."""

    oracle_should_irrigate: bool
    model_recommends_irrigate: bool


@dataclass(frozen=True)
class AsymmetricScore:
    weighted_cost: float  # lower is better; 0 is perfect
    recall_should_irrigate: float  # the gated number, kept near 1.0
    false_negatives: int  # missed irrigations (expensive)
    false_positives: int  # unnecessary irrigations (acceptable)
    passed: bool  # recall gate met


def score_decisions(decisions: list[IrrigationDecision]) -> AsymmetricScore:
    """Score a batch of irrigation decisions with the asymmetric cost + recall gate."""
    false_negatives = sum(
        1 for d in decisions if d.oracle_should_irrigate and not d.model_recommends_irrigate
    )
    false_positives = sum(
        1 for d in decisions if not d.oracle_should_irrigate and d.model_recommends_irrigate
    )
    weighted_cost = (
        false_negatives * MISSED_IRRIGATION_WEIGHT
        + false_positives * UNNECESSARY_IRRIGATION_WEIGHT
    )

    should = [d for d in decisions if d.oracle_should_irrigate]
    if should:
        caught = sum(1 for d in should if d.model_recommends_irrigate)
        recall = caught / len(should)
    else:
        # No days needed irrigation -> recall is vacuously perfect; the gate
        # can't fail on a batch with nothing to miss.
        recall = 1.0

    return AsymmetricScore(
        weighted_cost=weighted_cost,
        recall_should_irrigate=recall,
        false_negatives=false_negatives,
        false_positives=false_positives,
        passed=recall >= RECALL_GATE,
    )
