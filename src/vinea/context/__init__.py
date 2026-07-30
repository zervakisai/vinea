"""Counting the context before editing it, and bounding it afterwards.

Retrieval once tripled the irrigation leg's context in a single commit and nobody
noticed, because nothing in the system could see a token on a laptop: the
`input_tokens` column is populated by `MeteredModel`, which wraps only the gateway
path, so cost was visible in production and size was visible nowhere.

That gap is this package's reason to exist, and the ordering of its two modules is
the argument:

  `accounting`  measure. Offline, with no gateway, no model and no network, so
                the numbers are available *before* a change rather than after an
                invoice. Estimates, honestly labelled as estimates, with a
                calibration hook against the real counts already on the row.
  `budget`      bound. A token ceiling per leg, enforced by dropping whole
                low-ranked passages — never by truncating one, because a
                half-quoted source still carries a citation saying it is
                complete -- which is worse than no citation at all.

Nothing here computes an agronomic value, and nothing here is imported by the
protected core.
"""

from vinea.context.accounting import (
    CHARS_PER_TOKEN,
    ComponentSize,
    ContextReport,
    calibration_ratio,
    estimate_tokens,
    report_for_legs,
)
from vinea.context.budget import DEFAULT_LEG_TOKEN_BUDGET, fit_to_budget

__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_LEG_TOKEN_BUDGET",
    "ComponentSize",
    "ContextReport",
    "calibration_ratio",
    "estimate_tokens",
    "fit_to_budget",
    "report_for_legs",
]
