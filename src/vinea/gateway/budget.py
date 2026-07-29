"""Down and saying-no are different events. Telling them apart is the whole file.

Phase 8 built one degrade: no model, use the deterministic path. Phase 12 built
another: no registry, use the bundled prompt. Both are the same shape -- an
auxiliary system is unavailable, so fall toward the thing that always works.

A gateway breaks that symmetry, because it can fail in two ways that look
identical at the call site and must not share a code path:

  **The gateway is unreachable.** Connection refused, timeout, 502 from whatever
  sits in front of it. Nothing about the request was wrong. The correct degrade
  is the *direct provider*: the grower still gets a judged advisory, and we pay
  list price for the night instead of losing the judgement layer because a cache
  proxy fell over. `FallbackModel` handles this.

  **The gateway refuses on budget.** The request was understood, authenticated,
  and denied, because this tenant's virtual key has spent its allowance. Failing
  over to the direct provider here would route *around the control we just
  built* -- the budget would become advisory in the worst sense of the word,
  and the first month it mattered it would silently not matter. The correct
  degrade is the deterministic advisory: `degraded=True`, the grower gets real
  physics, and someone is told the tenant is out of budget.

So the ladder has two rungs and they lean opposite ways. Reachability failures
fall *toward* the model; policy failures fall *away* from it.

Why the budget lives in the gateway at all
------------------------------------------
`jobs/tenancy.py` counts calls in a `frozen` dataclass in worker memory. That is
a rule in code: it holds while the process lives, resets when it restarts, and
gives each of two workers its own full allowance. Moving the ceiling onto a
LiteLLM virtual key makes it a rule in the one system that sees every call and
persists the tally -- the third time this project has traded a promise for a
guarantee (the others: the unique index behind advisory idempotency, and the
partial index behind one-open-config-per-block).
"""

from __future__ import annotations

from pydantic_ai.exceptions import ModelHTTPError

# Substrings LiteLLM puts in the body when a key or team has spent its
# allowance. Matched case-insensitively against `str(exc)`, which for
# ModelHTTPError includes the body.
#
# Text matching is fragile and this file should say so rather than pretend
# otherwise: LiteLLM answers both "you are out of budget" and "you are going too
# fast" with the same status code in some versions, and only the body separates
# them. The fragility is bounded by which way it fails -- an unrecognised budget
# refusal falls over to the direct provider, i.e. it degrades toward spending
# money, which is exactly why the gateway ALSO enforces the ceiling itself. This
# check decides how we *react*; it is not the control.
_BUDGET_MARKERS = (
    "budget has been exceeded",
    "exceeded budget",
    "budget_exceeded",
    "max_budget",
    "budgetexceedederror",
)


class BudgetRefused(RuntimeError):
    """The gateway refused because a spend ceiling was reached.

    Raised in place of the provider error so the worker can branch on a type
    rather than re-parse the message. Carries the tenant when known, because the
    operator's next question is always "which one?".
    """

    def __init__(self, message: str, *, tenant: str | None = None) -> None:
        super().__init__(message)
        self.tenant = tenant


def is_budget_refusal(exc: BaseException) -> bool:
    """True when this exception is the gateway declining to spend more.

    Deliberately narrow. Everything it does not recognise is treated as an
    outage, which fails over to the direct provider -- so a false negative costs
    money and a false positive costs a grower their judged advisory. Given those
    two, this errs toward the false negative, and the gateway's own ceiling is
    what stops that from being unbounded.
    """
    if isinstance(exc, BudgetRefused):
        return True
    if not isinstance(exc, ModelHTTPError):
        return False
    # 401/403 are "your key is wrong", not "your key is spent"; they are a
    # deployment fault and must not be dressed up as a budget event.
    if exc.status_code not in (400, 429):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _BUDGET_MARKERS)


def should_fall_back(exc: Exception) -> bool:
    """`FallbackModel`'s predicate: may this error try the direct provider?

    The one thing it must refuse is a budget refusal. Everything else that the
    SDK classifies as a model API error -- connection, timeout, 5xx, and 429s
    that are ordinary rate limits -- is a reachability problem and is allowed to
    fall through to the provider.
    """
    return not is_budget_refusal(exc)
