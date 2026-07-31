"""Scoring for retrieval: recall@k and MRR over chapter-labelled questions.

One definition, two callers. `scripts/measure_retrieval.py` uses it to compare
variants and produce the numbers that appear in `rag/store.py` and ADR-011;
`tests/test_rag.py` uses it for the gate. A harness and a gate that each implement
their own arithmetic will disagree eventually, and the disagreement will be found
by whoever is trying to work out why a change that measured better went red.

The questions themselves stay in `tests/fixtures/` and are passed in. This module
knows how to score; it does not know what the questions are, which keeps the
package from reaching into the test tree.

## Why MRR is here at all

The gate is recall@3, because three passages is what an agent is shown. But recall@3
over 27 questions moves in steps of 0.037, so a variant can "win" by flipping one
question -- and a one-question win over a fixed question set is a coin toss with a
decimal point. MRR reads the whole ranking, so a variant that orders better shows it
even when nothing crosses the boundary.

That distinction decided the shipped query. Length normalisation and
locator-in-the-tsvector both scored 0.81 recall@3, indistinguishable; MRR separated
them by 0.07 on the paraphrased half and picked the right one.

## Ground truth is a chapter

Chunk ids are assigned in file order by the corpus builder, so they move whenever
the chunker or the upstream extraction changes. A gate keyed on them would go red
on every corpus regeneration for reasons that have nothing to do with retrieval,
and a gate that cries wolf gets deleted rather than investigated.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Deep enough for MRR to tell a near miss from a total one, and no deeper: a correct
# passage at rank 40 contributes 0.025, which is not a difference anyone would feel.
DEFAULT_DEPTH = 20


def chapter_of(locator: str) -> set[int]:
    """The chapter a locator names, or the empty set.

    A set rather than an int|None so a locator that names no chapter -- front
    matter, an annex, a figure caption that lost its heading -- contributes nothing
    to the match instead of a wrong number that would silently score as a hit.
    """
    if not locator.startswith("Chapter "):
        return set()
    try:
        return {int(locator.split()[1].rstrip(":—-"))}
    except (IndexError, ValueError):
        return set()


def first_correct_rank(locators: Iterable[str], wanted: set[int]) -> int | None:
    """1-based rank of the first passage from a wanted chapter, or None."""
    for rank, locator in enumerate(locators, 1):
        if chapter_of(locator) & wanted:
            return rank
    return None


@dataclass(frozen=True, slots=True)
class RetrievalScore:
    """What one variant did over one set of questions."""

    questions: int
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    # `id` when nothing correct was found at all, `id@rank` when it was found but
    # below rank 3. The distinction is the whole diagnosis: one is "the corpus does
    # not answer this", the other is "the ranking buried it".
    outside_top_3: tuple[str, ...]

    @property
    def summary(self) -> str:
        return (
            f"n={self.questions} r@1={self.recall_at_1:.2f} r@3={self.recall_at_3:.2f} "
            f"r@5={self.recall_at_5:.2f} MRR={self.mrr:.3f}"
        )


def score_ranked_results(
    results: Sequence[tuple[str, Sequence[str]]],
    wanted: Sequence[set[int]],
) -> RetrievalScore:
    """Score already-retrieved rankings.

    Takes results rather than running the search, so the caller decides what query
    to measure -- which is what lets one function score both the shipped query and a
    candidate variant without either knowing about the other.

    `results` is (question_id, ordered locators); `wanted` is the chapter set for
    each, positionally.
    """
    if len(results) != len(wanted):
        raise ValueError(f"{len(results)} result sets for {len(wanted)} questions")

    hits = {1: 0, 3: 0, 5: 0}
    reciprocal = 0.0
    outside: list[str] = []

    for (question_id, locators), chapters in zip(results, wanted, strict=True):
        rank = first_correct_rank(locators, chapters)
        if rank is None:
            outside.append(question_id)
            continue
        reciprocal += 1.0 / rank
        for k in hits:
            hits[k] += rank <= k
        if rank > 3:
            outside.append(f"{question_id}@{rank}")

    n = len(results) or 1
    return RetrievalScore(
        questions=len(results),
        recall_at_1=hits[1] / n,
        recall_at_3=hits[3] / n,
        recall_at_5=hits[5] / n,
        mrr=reciprocal / n,
        outside_top_3=tuple(outside),
    )
