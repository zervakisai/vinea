"""What one advisory's model calls consumed. Two sources, kept apart on purpose.

An advisory is three model calls (irrigation, spray, coordinator) and the thing
worth storing is their *total*. Two different sources contribute to it, and the
difference between them is this module's lesson:

  **Tokens** come from the SDK. `ModelResponse.usage` is populated by every
  provider on every response, gateway or not. They are ours to count, always.

  **Cost and cache-hit** come from the gateway, in response *headers*
  (`x-litellm-response-cost`, `x-litellm-cache-hit`). The SDK's typed
  `ModelResponse` has no room for a vendor's out-of-band metadata, and it should
  not -- so these arrive through an httpx event hook on the client we inject,
  and they arrive only when a gateway is in the path.

Which means, without a gateway, tokens are known and cost is NULL. That is not a
gap to paper over with `tokens x price_table` -- it is the house rule working:
a column that arrives empty is honest; a fabricated default is not.

The two sources are recorded *independently* and summed separately, never paired
up call-by-call. Pairing would need the hook and the response to agree on which
call they belong to, which is a correlation problem with no correlation id, and
would break the moment the graph ran two agents concurrently. Totals need no
pairing, and totals are all anyone asks for.

Scoping is a `ContextVar` holding a *mutable* list, the same trick pydantic-ai's
own `capture_run_messages` uses: children that `asyncio.gather` inherit a copy of
the context, but the copy points at the same list, so their appends are visible
to the parent. A `.set()` from a child would not be.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class Ledger:
    """The mutable tally for one advisory run. Append-only; read at the end."""

    input_tokens: int = 0
    output_tokens: int = 0
    # None until a gateway reports a cost. Stays None for the whole run if no
    # gateway was in the path -- see the module docstring.
    cost_usd: float | None = None
    # One entry per gateway response, True when it was served from the gateway's
    # exact-match cache.
    cache_flags: list[bool] = field(default_factory=list)
    # How many model requests the wrapper saw. Distinguishes "no gateway" from
    # "no calls at all" when reading the ledger back.
    calls: int = 0
    # Characters of the fully-assembled request, measured at the same
    # instant as the token count above. Paired numbers from one request are what
    # make a chars-per-token calibration a measurement rather than a restatement
    # of the assumption it is meant to check.
    prompt_chars: int = 0
    # `advisories.model_id` records the
    # gateway ALIAS (`vinea-nightly`), which is the point of an alias and also
    # weakens one of the five drift tags: a year later, "which model produced
    # this?" needs the gateway's own logs. The provider echoes a model name in
    # every response; this records the LAST one seen, which for a single-model
    # advisory is the model that served it.
    #
    # Honest limit: what LiteLLM echoes depends on its configuration. If it
    # returns the alias, this records the alias and the tag is no better than it
    # was -- but the mechanism is in place and the failure is visible in a column
    # rather than invisible in a design.
    resolved_model: str | None = None

    def record_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        prompt_chars: int = 0,
        model_name: str | None = None,
    ) -> None:
        """From the SDK's `ModelResponse.usage`, plus what we sent to earn it."""
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.prompt_chars += prompt_chars
        if model_name:
            self.resolved_model = model_name

    def record_gateway_headers(self, *, cost_usd: float | None, cache_hit: bool | None) -> None:
        """From the gateway's response headers. Available only behind a gateway."""
        if cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + cost_usd
        if cache_hit is not None:
            self.cache_flags.append(cache_hit)


@dataclass(frozen=True, slots=True)
class RunCost:
    """The immutable read-out, shaped for the four columns on `advisories`."""

    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    cache_hit: bool | None
    # NULL alongside input_tokens, never on its own: the pair is the
    # point, and a character count with no token count beside it calibrates
    # nothing.
    context_chars: int | None = None
    # What the provider said served the call, when it says anything. `model_id`
    # on the row records what was *asked* for, which behind a gateway is an alias.
    served_model: str | None = None

    @classmethod
    def from_ledger(cls, ledger: Ledger) -> RunCost:
        """Collapse a run's calls into one row's worth of evidence.

        `cache_hit` is True only when **every** call in the run was served from
        cache, because the column answers "did this advisory cost anything new?"
        and two-thirds cached still bought a completion. An `any()` here would
        make the cache look better than it is, on a column an operator uses to
        decide whether caching is worth keeping.
        """
        if ledger.calls == 0:
            # No model call happened at all: the router skipped it, or there was
            # no key. Zeros would claim a call was made and was free.
            return cls(input_tokens=None, output_tokens=None, cost_usd=None, cache_hit=None)
        return cls(
            input_tokens=ledger.input_tokens,
            output_tokens=ledger.output_tokens,
            cost_usd=ledger.cost_usd,
            cache_hit=all(ledger.cache_flags) if ledger.cache_flags else None,
            context_chars=ledger.prompt_chars or None,
            served_model=ledger.resolved_model,
        )


_ledger: ContextVar[Ledger | None] = ContextVar("vinea_gateway_ledger", default=None)


def current_ledger() -> Ledger | None:
    """The ledger for the run in progress, or None outside a `ledger_scope`.

    None is the normal case for a CLI run or a test: nothing is collecting, so
    the recorders below become no-ops and cost the caller a contextvar read.
    """
    return _ledger.get()


@contextmanager
def ledger_scope() -> Iterator[Ledger]:
    """Collect usage for everything that runs inside this block.

    Nesting is not supported and does not need to be -- one advisory, one scope.
    The token reset in `finally` is what keeps a worker's second task from
    inheriting the first task's tally.
    """
    ledger = Ledger()
    token = _ledger.set(ledger)
    try:
        yield ledger
    finally:
        _ledger.reset(token)
