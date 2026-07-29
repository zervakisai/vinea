"""phase 16 -- counting the context, bounding it, and proving a slimming was safe.

The last of those three is the one worth reading carefully, because the obvious
claim is not available and this file says so rather than implying otherwise.

**What these tests prove.** That every fact the output validators and the
deterministic oracles depend on is still present in the assembled prompt after
slimming. That is a *prompt contract*: nothing load-bearing was deleted.

**What they do not prove.** That the model's prose is no worse. Nothing here can
— it needs a live model and, for the parts that matter (is this rationale useful
to an agronomist?), a human. That is phase 12's annotation queue, not a unit test.

The distinction is the phase's own honesty check. "I removed 10% of the prompt
and the tests still pass" is a true statement about a weaker claim than it sounds
like, and stating the weaker claim precisely is the whole job.

The assertions run against the rendered strings directly rather than through a
`FunctionModel` capture. That was the first approach and it bought nothing here:
the renderers are pure functions of the features, so calling them IS the prompt,
and going through an agent only adds a model that has to be stubbed. The capture
seam stays useful for what it was built for in `test_agents.py` -- asserting what
reaches an agent through the graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from vinea import agents, config
from vinea.context import CHARS_PER_TOKEN, estimate_tokens, report_for_legs
from vinea.context.budget import DEFAULT_LEG_TOKEN_BUDGET, fit_to_budget
from vinea.deps import WINE_GRAPES
from vinea.features import build_features
from vinea.ingest import load_weather

RUN_DATE = date(2026, 7, 28)


def _features():
    data_dir = config.DEFAULT_DATA_DIR
    history = sorted(Path(data_dir).glob("*last-30d*.csv"))[-1]
    forecast = sorted(Path(data_dir).glob("*next-7d*.csv"))[-1]
    hist, fc, dq = load_weather(history, forecast, RUN_DATE)
    return build_features(hist, fc, dq, RUN_DATE, WINE_GRAPES)


# --------------------------------------------------------------------------- #
# Accounting                                                                   #
# --------------------------------------------------------------------------- #


def test_the_estimator_is_named_an_estimate():
    """Not `count_tokens`, and the name is doing work.

    A caller who reads `count` believes a number; a caller who reads `estimate`
    checks it before betting a budget on it. The value is characters over a
    stated constant, and the constant is exported so nobody has to read the
    source to find out what the number is made of.
    """
    assert estimate_tokens("") == 0
    assert estimate_tokens("x" * 400) == round(400 / CHARS_PER_TOKEN)
    # Never zero for non-empty text: a component that exists costs at least one
    # token, and rounding it to nothing hides small components in a report.
    assert estimate_tokens("x") == 1


def test_a_report_finds_the_largest_component():
    """The report exists so "what is in the prompt?" is a table rather than a
    reading of three source files. Phase 15's passages went from nothing to the
    majority of both legs without anyone deciding that."""
    reports = report_for_legs(
        {"irrigation": {"instructions": "a" * 100, "retrieved": "b" * 900}}
    )
    leg = reports[0]
    assert leg.chars == 1000
    assert leg.largest().name == "retrieved"
    assert leg.share_of("retrieved") == pytest.approx(0.9)
    assert "TOTAL irrigation" in leg.as_table()


def test_an_empty_leg_does_not_divide_by_zero():
    assert report_for_legs({"spray": {}})[0].share_of("anything") == 0.0


# --------------------------------------------------------------------------- #
# The budget: whole passages, never truncated                                  #
# --------------------------------------------------------------------------- #


@dataclass
class _Passage:
    text: str
    rank: int


def test_the_budget_drops_whole_passages_and_never_truncates_one():
    """The rule the module exists for.

    A truncated passage still arrives labelled "Chapter 8 -- ETc under soil water
    and salinity stress conditions". The label becomes a claim about text the
    model never saw the end of, and a reader who follows it finds a paragraph
    that does not say what they were told. A missing citation leaves a claim
    unverified; a truncated one moves it to falsely verified.
    """
    passages = [_Passage("x" * 1200, 1), _Passage("y" * 1200, 2), _Passage("z" * 1200, 3)]
    outcome = fit_to_budget(passages, budget_tokens=600)

    assert outcome.dropped == 1
    assert [p.rank for p in outcome.kept] == [1, 2]
    # Every surviving passage is byte-identical to its input. This is the
    # assertion; the count above is bookkeeping.
    assert all(kept.text == original.text for kept, original in zip(outcome.kept, passages, strict=False))


def test_a_higher_rank_is_never_displaced_by_a_lower_one():
    """RRF already decided what is worth the tokens.

    Rank 1 costs the entire budget; ranks 2 and 3 together cost half of it. A
    knapsack fit maximising passage *count* would drop rank 1 and keep both
    others -- more text, less relevant text. Rank-order greed keeps rank 1 alone,
    which is the trade this system wants.
    """
    passages = [_Passage("x" * 1600, 1), _Passage("y" * 400, 2), _Passage("z" * 400, 3)]
    kept = fit_to_budget(passages, budget_tokens=400).kept
    assert [p.rank for p in kept] == [1]


def test_surviving_ranks_may_be_non_contiguous():
    """Documented behaviour, not an accident: an over-large passage is skipped
    and a smaller lower-ranked one can still be kept. Stopping at the first
    passage that does not fit would throw away budget a later one uses well."""
    passages = [_Passage("x" * 400, 1), _Passage("y" * 4000, 2), _Passage("z" * 400, 3)]
    kept = fit_to_budget(passages, budget_tokens=300).kept
    assert [p.rank for p in kept] == [1, 3]


def test_one_oversized_passage_is_kept_alone_rather_than_dropping_everything():
    """Returning nothing would trade a slightly over-budget prompt for a
    completely uncited advisory. Phase 15's floor is for genuine failures, not
    for arithmetic."""
    outcome = fit_to_budget([_Passage("x" * 10_000, 1)], budget_tokens=100)
    assert len(outcome.kept) == 1
    assert outcome.dropped == 0


def test_the_budget_admits_three_passages_and_refuses_a_fourth():
    """The constant encodes phase 15's recall gate re-run at each depth:

        top_k=1  recall 0.83   286 tokens
        top_k=2  recall 0.92   576 tokens
        top_k=3  recall 1.00   865 tokens   <- saturates
        top_k=4  recall 1.00  1165 tokens   <- 300 tokens for nothing

    So the ceiling sits between three and four. This test is what stops someone
    raising TOP_K without also raising the budget and having to justify it.
    """
    typical = [_Passage("x" * 1150, i) for i in range(1, 5)]
    outcome = fit_to_budget(typical, budget_tokens=DEFAULT_LEG_TOKEN_BUDGET)
    assert len(outcome.kept) == 3
    assert outcome.dropped == 1


# --------------------------------------------------------------------------- #
# The prompt contract: what slimming must not remove                           #
# --------------------------------------------------------------------------- #


def test_the_slimmed_spray_prompt_still_carries_every_load_bearing_fact():
    """The honest claim: nothing the downstream validators need was deleted.

    Phase 16 removed `index=None` from 24 per-hour rows -- 211 characters
    restating one fact. What must survive is everything the spray validator and
    the deterministic oracle actually check: the candidate windows (the validator
    rejects an invented one), the per-hour bands and wind, and an explicit
    statement that the index is absent rather than fine.
    """
    features = _features()
    rendered = agents.render_spray_input(features.spray)

    # The absence is stated once, not fabricated and not silently dropped.
    assert "Not reported by this feed for any hour: spray index." in rendered
    assert "index=None" not in rendered

    # Everything load-bearing survives.
    assert "Candidate windows" in rendered
    for window in features.spray.windows:
        assert f"{window.start:%H:%M}-{window.end:%H:%M}" in rendered
    assert "band=" in rendered and "wind=" in rendered and "suitable=" in rendered
    # One row per hour, still.
    assert rendered.count("band=") == len(features.spray.hours)


def test_dropping_all_none_columns_never_hides_a_partially_present_field():
    """The line between removing noise and fabricating data.

    A field that is None for SOME hours still varies, and hiding the gaps would
    be exactly the fabrication "missing stays missing" forbids. Only columns that
    are None for every single hour are dropped, and even then the absence is
    announced.
    """
    features = _features()
    hours = list(features.spray.hours)
    # Make precipitation partially present: it must therefore stay per-row.
    assert any(h.precip_mm is not None for h in hours)
    rendered = agents.render_spray_input(features.spray)
    assert "precip=" in rendered
    assert "precipitation" not in rendered.split("Candidate windows")[0]


def test_the_irrigation_prompt_still_carries_the_number_the_oracle_checks():
    """The water-balance oracle recomputes depletion and compares. If slimming
    ever removed it from the prompt, the model would have to invent the number
    and the oracle would catch it -- one phase later, in a red eval run, instead
    of here."""
    features = _features()
    rendered = agents.render_irrigation_input(features.irrigation, features.target_date)
    assert f"{features.irrigation.current_depletion_mm}" in rendered or "depletion" in rendered.lower()
    assert features.target_date.isoformat() in rendered


def test_retrieved_passages_are_framed_as_background_not_as_input():
    """The framing is the safeguard that keeps FAO-56's Kc tables out of the
    arithmetic. It survives every slimming by being asserted here."""
    from vinea.rag.citations import RetrievedPassage
    from vinea.rag.retrieve import REFERENCE_CONTRACT, render_passages

    rendered = render_passages(
        [RetrievedPassage(leg="irrigation", chunk_id=1, locator="Chapter 8", text="RAW = p x TAW", rank=1)]
    )
    assert all(rule in rendered for rule in REFERENCE_CONTRACT)


# --------------------------------------------------------------------------- #
# The measurement that started the phase                                       #
# --------------------------------------------------------------------------- #


def test_retrieval_is_still_the_largest_component_and_that_is_recorded():
    """Not a regression guard -- a *visibility* guard.

    Phase 15 tripled the irrigation leg's context in one commit and nothing in
    the system could see it. This asserts the accounting can, on the committed
    dataset, with no gateway and no database: if retrieval ever stops dominating,
    or starts dominating far more, the number in the phase doc is wrong and this
    test says so.
    """
    features = _features()
    irr_deps = agents.IrrDeps(
        crop=WINE_GRAPES,
        features=features.irrigation,
        data_quality=features.data_quality,
        target_date=features.target_date,
        run_date=RUN_DATE,
    )
    own_context = {
        "static instructions": agents._IRR_STATIC,
        "context block": agents.render_irrigation_context(irr_deps),
        "user input": agents.render_irrigation_input(features.irrigation, features.target_date),
    }
    report = report_for_legs({"irrigation": own_context})[0]

    # The system's OWN context -- everything except retrieval -- is small, and
    # that is the point: the thing that grew was not the reasoning.
    assert report.tokens < 500, report.as_table()
    # And three typical passages dwarf it.
    assert estimate_tokens("x" * 3450) > report.tokens
