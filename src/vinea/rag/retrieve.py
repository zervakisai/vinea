"""The facade the agents call — and the floor it falls to.

`retrieve_for` is the whole public surface, and it **never raises**. The ladder
is deliberately shorter than the prompt registry's, because the correct floor here
is emptier:

  1. Retrieval works  -> passages, recorded in the citation ledger.
  2. Anything at all goes wrong -> **no passages**, and the advisory is produced
     exactly as it was in.

There is no "serve a weaker passage" rung, and that asymmetry is the point. Every
other fail-open path in this system degrades toward a *correct but lesser* answer:
the deterministic advisory, the bundled prompt, a NULL cost. A citation is
different, because an unfounded one is not a lesser claim — it is a *stronger*
one. It moves a statement from unverified to falsely verified and points a grower
at a page that does not say what they were told it says.

So the floor is silence. A missing citation is honest; a weak one is not.

What this module is forbidden to do
-----------------------------------
Retrieval runs strictly downstream of `features.build_features` and its results
reach only the *instructions* given to an agent — prose about prose. No retrieved
text may reach a computed value. The corpus is FAO-56, which is where Kc tables
and `RAW = TAW x MAD` live, so the temptation is concrete: a passage stating a
crop coefficient sits one line away from `Deps.kc`. `Deps` wins, always, and it
is not a negotiation the model is invited into.
"""

from __future__ import annotations

import logging

from vinea.context.budget import DEFAULT_LEG_TOKEN_BUDGET, fit_to_budget
from vinea.rag.citations import RetrievedPassage, current_citations

logger = logging.getLogger(__name__)

# The corpus identifier stored on every row. One constant so the ingest CLI, the
# query and the tests cannot drift apart on a string literal.
SOURCE = "fao56"

# The instruction that separates reference material from input data.
#
# A named constant rather than a literal inside `render_passages`, because the
# tests that assert this framing survives are asserting a *contract*, not a
# sentence -- pinning the prose makes any rewording a red build for no reason,
# and encourages the next person to delete the assertion instead of the risk.
#
# Reworded freely; what must not change is that it (a) marks the text as
# background, (b) forbids taking numbers from it, and (c) says the configuration
# wins when they disagree. `tests/test_rag.py` checks those three properties.
REFERENCE_PREAMBLE = (
    "REFERENCE MATERIAL — FAO Irrigation and Drainage Paper 56 (CC BY 4.0).\n"
    "Use these passages ONLY to explain and attribute your reasoning in prose. "
    "They are background, not inputs. Every numeric value you report must come "
    "from the computed features supplied above; if a passage states a coefficient "
    "or a threshold that differs from the configuration you were given, the "
    "configuration is correct and the passage is context. Do not recompute anything."
)

# The three properties that framing must keep, whatever the wording. Tests assert
# against these rather than against the sentence.
REFERENCE_CONTRACT = (
    "background, not inputs",       # marked as reference, not data
    "Do not recompute",             # no arithmetic on retrieved numbers
    "configuration is correct",     # config wins any disagreement
)

# How many passages the *query* returns. Three was chosen because three is a
# reasonable-looking number; measuring what that bought gave 4 217
# characters, 64% of the irrigation leg's entire context.
#
# It stays at three, and the ceiling moved to a different unit. A count is the
# wrong unit for a context budget because passages are not the same length --
# chunks run 200 to 1 200 characters, so `TOP_K = 3` buys anywhere from 600 to
# 3 600. `DEFAULT_LEG_TOKEN_BUDGET` is the real bound; this is now just how deep
# the ranking is read before the budget decides.
TOP_K = 3


# One engine per database URL, reused across advisories.
#
# `db.session` deliberately builds a fresh engine per caller so nothing owns a
# process-wide pool -- right for the API and the worker, which each create one
# and hold it for their lifetime. It is wrong here: `retrieve_for` runs twice per
# advisory from inside the graph, so `make_engine()` per call meant a new
# connection pool per call, used for one query and dropped without being
# disposed. Keyed on the URL so a test that repoints DATABASE_URL gets its own.
_ENGINES: dict[str, object] = {}


def _engine():
    from vinea.db.session import database_url, make_engine

    url = database_url()
    if url not in _ENGINES:
        _ENGINES[url] = make_engine(url)
    return _ENGINES[url]


def reset_engine_cache() -> None:
    """Dispose and forget the cached engines. For tests that change the URL."""
    for engine in _ENGINES.values():
        engine.dispose()
    _ENGINES.clear()


def retrieve_for(
    leg: str,
    query: str,
    *,
    top_k: int = TOP_K,
    budget_tokens: int = DEFAULT_LEG_TOKEN_BUDGET,
) -> list[RetrievedPassage]:
    """Passages for one leg's question, or an empty list. Never raises.

    Imports are function-local so that a deployment with no database reachable,
    or without the `rag` extra installed, still imports `agents.py` and produces
    advisories. The cost is a few microseconds per call; the benefit is that
    retrieval cannot break the deterministic path by being absent.
    """
    try:
        from sqlmodel import Session

        from vinea.rag.store import search

        with Session(_engine()) as session:
            hits = search(session, query, source=SOURCE, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 -- the floor is silence, see the module docstring
        # Debug, not warning. On a laptop with no corpus ingested this is the
        # normal state, and a nightly WARNING that means "the optional thing is
        # optional" is how people learn to ignore logs.
        logger.debug("retrieval unavailable for leg %s: %s: %s", leg, type(exc).__name__, exc)
        return []

    passages = [
        RetrievedPassage(leg=leg, chunk_id=h.chunk_id, locator=h.locator, text=h.text, rank=rank)
        for rank, h in enumerate(hits, start=1)
    ]

    # A token ceiling, enforced by dropping whole low-ranked passages.
    # Applied HERE rather than in `render_passages` so that what gets cited is
    # exactly what got shown -- trimming after the ledger recorded them would
    # produce citations for passages the model never saw, which is the same
    # falsely-verified failure this system keeps arranging itself against.
    outcome = fit_to_budget(passages, budget_tokens=budget_tokens)
    if outcome.trimmed:
        logger.debug(
            "leg %s: dropped %d of %d passages to fit %d tokens (%d -> %d)",
            leg, outcome.dropped, len(passages), budget_tokens,
            outcome.tokens_before, outcome.tokens_after,
        )
    passages = outcome.kept

    ledger = current_citations()
    if ledger is not None:
        ledger.record(passages)
    return passages


def render_passages(passages: list[RetrievedPassage]) -> str:
    """The passages as instruction text, or an empty string.

    The framing is not decoration. Without it a model reads retrieved text as
    additional *input data* and starts arithmetic on any number it contains —
    which for this corpus means reading a Kc out of a table and using it. The
    instruction says, in the imperative, that these are for explanation only and
    that every number in the advisory comes from the features it was given.

    An empty list renders to an empty string rather than "no sources found",
    because a sentence about the absence of sources is itself a thing a model
    will try to be helpful about.
    """
    if not passages:
        return ""

    body = "\n\n".join(f"[{p.rank}] {p.locator}\n{p.text}" for p in passages)
    return (
        f"{REFERENCE_PREAMBLE}\n\n{body}"
    )
