"""A ceiling on retrieved context, enforced by dropping whole passages.

Phase 15 handed this phase a bill and a warning in the same comment:

    TOP_K = 3   # rag/retrieve.py
    # ...this is exactly the kind of constant that grows to ten because nobody
    # measured what it cost.

Measured: three passages are 4 217 characters, 64% of the irrigation leg's entire
context. A *count* is the wrong unit for that ceiling, because passages are not
the same length — chunks run from 200 to 1 200 characters, so `TOP_K = 3` buys
anywhere from 600 to 3 600. A token budget is the unit that means something.

## Whole passages, never truncated

The one rule this module exists to enforce. When the budget is exceeded, the
lowest-ranked passage is dropped **entirely**; nothing is cut mid-passage.

That is not tidiness. Phase 15's promise is that a citation points at something a
reader can go and check, and a truncated passage still arrives labelled
"Chapter 8 — ETc under soil water and salinity stress conditions". The label is
now a claim about text the model never saw the end of. A reader who follows it
finds a paragraph that does not say what the advisory said it says — and
concludes the citations are unreliable *in general*, which is the correct
inference from their point of view and a disaster from ours.

A missing citation leaves a claim unverified, and a reader knows to be sceptical.
A truncated one moves the claim to falsely verified. Same asymmetry phase 15 built
its fail-open floor on, one layer up.

## Why not summarise instead

Summarising passages to fit would preserve the count and lose the same guarantee
by a subtler route: the citation would then point at a source the *summary* was
drawn from, and the model would be quoting itself with FAO's name attached.
Rejected for the same reason ADR-007 rejected semantic caching — the failure is
not staleness, it is a confident wrong answer that no amount of care in the
mechanism can rule out.
"""

from __future__ import annotations

from dataclasses import dataclass

from vinea.context.accounting import estimate_tokens

# Tokens of *retrieved* passage text allowed per leg. Not the whole prompt: the
# instructions, the context block and the input are the system's own reasoning
# and are not negotiable against a corpus. Not the framing either, which is a
# fixed ~125 tokens whatever survives.
#
# 900 is not a round number and it does not trim anything today. It encodes a
# measurement — phase 15's recall gate, re-run at each depth:
#
#   top_k=1   recall 0.83   286 tokens
#   top_k=2   recall 0.92   576 tokens
#   top_k=3   recall 1.00   865 tokens     <- saturates here
#   top_k=4   recall 1.00  1165 tokens     <- 300 tokens for nothing
#
# So the ceiling sits above three passages and below four. Dropping to two would
# save 289 tokens and cost eight points of recall; paying for a fourth buys
# literally nothing measurable. That is the answer to "is TOP_K = 3 right?" —
# yes, and now for a reason rather than because three looks reasonable.
#
# Which makes this budget's job forward-looking, not corrective. It exists so
# that a later phase raising `TOP_K`, or a corpus whose chunks run longer, cannot
# grow the prompt without someone deliberately raising this number and having to
# justify it against the table above.
DEFAULT_LEG_TOKEN_BUDGET = 900


@dataclass(frozen=True, slots=True)
class BudgetOutcome:
    """What fitting did, so a caller can log it rather than guess.

    `dropped` is the count, not a flag. "Retrieval was trimmed" is not actionable;
    "two of three passages were dropped every night for a month" is a signal that
    the budget and `TOP_K` disagree and one of them should change.
    """

    kept: list
    dropped: int
    tokens_before: int
    tokens_after: int

    @property
    def trimmed(self) -> bool:
        return self.dropped > 0


def fit_to_budget(passages: list, *, budget_tokens: int = DEFAULT_LEG_TOKEN_BUDGET) -> BudgetOutcome:
    """Keep the best-ranked passages that fit. Drop the rest whole.

    Considered strictly in rank order, so a higher-ranked passage is never
    displaced to make room for lower-ranked ones — RRF has already decided what
    is worth the tokens, and a knapsack fit that traded rank 1 for two of rank 4
    would optimise the wrong quantity: more text, less relevant text.

    It does `continue` rather than `break`, though, and that is a real behaviour
    worth knowing about: an over-large passage is skipped and a smaller
    lower-ranked one may still be kept. So the surviving ranks can be
    non-contiguous — {1, 3} rather than {1, 2}. Nothing downstream assumes
    otherwise (`advisory_citations.rank` records the original position), and the
    alternative — stopping at the first passage that does not fit — throws away
    budget that a later passage would have used well.

    A single passage larger than the whole budget is still kept, alone. The
    alternative is returning nothing, which trades a slightly over-budget prompt
    for a completely uncited advisory — and phase 15's floor exists for genuine
    failures, not for arithmetic.
    """
    kept: list = []
    used = 0
    before = sum(estimate_tokens(getattr(p, "text", "")) for p in passages)

    for passage in passages:
        cost = estimate_tokens(getattr(passage, "text", ""))
        if kept and used + cost > budget_tokens:
            continue
        kept.append(passage)
        used += cost

    return BudgetOutcome(
        kept=kept,
        dropped=len(passages) - len(kept),
        tokens_before=before,
        tokens_after=used,
    )
