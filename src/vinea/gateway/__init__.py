"""The LLM gateway seam: routing, failover, and the evidence of what a call cost.

Four things live here and nothing else does:

  `settings`  reads the environment and answers one question -- is a gateway
              configured at all? Everything downstream branches on that, and the
              *unset* answer must leave the system exactly as it was without one.
  `ledger`    a per-run tally of what the model calls actually consumed. Tokens
              come from the SDK, cost and cache-hit come from the gateway's
              response headers, and neither is ever invented.
  `routing`   `resolve_model()` -- the one function the agents call. It returns
              the plain model string when no gateway is configured, and a
              metered, failover-wrapped Model when one is.
  `budget`    the distinction that decides how a refusal degrades: a gateway that
              is *down* and a gateway that is *saying no* are different events and
              must not share a code path.

What is deliberately NOT here: a price table. Cost is read from the gateway
because the gateway is the thing that knows what was charged; recomputing it from
tokens later would produce a number that was never on anyone's invoice (ADR-007).
"""

from vinea.gateway.budget import BudgetRefused, is_budget_refusal
from vinea.gateway.ledger import RunCost, current_ledger, ledger_scope
from vinea.gateway.routing import resolve_model
from vinea.gateway.settings import GatewaySettings, gateway_settings

__all__ = [
    "BudgetRefused",
    "GatewaySettings",
    "RunCost",
    "current_ledger",
    "gateway_settings",
    "is_budget_refusal",
    "ledger_scope",
    "resolve_model",
]
