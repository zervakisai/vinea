"""`resolve_model()` — the one function the agents call, and the ladder behind it.

The seam this uses was drawn in phase 3 for a different reason. `agents.py` binds
the model at **run time**, not at construction, so that importing the module
needs no API key and tests can override it. That decision now carries the
gateway, which is the same payoff ADR-002's `WeatherSource` gave: a seam drawn
for one reason paying out for another.

The ladder, and note which way each rung leans:

  1. **No gateway configured.** Return the plain `config.MODEL` *string*, exactly
     what phase 3 passed. Not a wrapper, not an object -- the type does not even
     change. A laptop with an `ANTHROPIC_API_KEY` behaves as it did in phase 13,
     and the four cost columns stay NULL, which is the honest report: nothing was
     in the path that knew what anything cost.

  2. **Gateway configured, no direct provider key.** Route everything through the
     gateway, metered. If the gateway is down there is nowhere to fall to, and
     the worker degrades to the deterministic advisory.

  3. **Gateway configured, direct provider key also present.** As above, plus
     `FallbackModel(gateway, direct)`: a gateway *outage* falls through to the
     provider and the grower still gets a judged advisory.

Rung 3 is not free, and the price is the interesting part. The reason to run a
gateway with virtual keys is that the deployment never holds a provider key --
a leaked virtual key can spend its budget and no more. Keeping a provider key
beside it for failover puts the unbounded credential back in the pod. So the
choice is real and it is the operator's:

    survives a gateway outage   <->   only one bounded key to leak

The chart makes it a values flag rather than a code path, and neither answer is
wrong. What would be wrong is having the fallback silently depend on a key
someone left in a Secret for an unrelated reason -- so rung 2 and rung 3 are
distinguished explicitly here, and the worker records which one ran.

Why the model object is rebuilt per event loop
----------------------------------------------
`graph.run_advisory_sync` is `asyncio.run(...)`, once per advisory. An
`httpx.AsyncClient` binds its connection pool to the loop that first used it, so
a client cached across advisories would be talking to a closed loop by the second
one. The cache below is therefore keyed by the *running loop*, single-slot: the
three agent calls of one advisory share a client, the next advisory gets a fresh
one, and the previous is collected with its dead loop.
"""

from __future__ import annotations

import asyncio

import httpx
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from vinea import config
from vinea.gateway.budget import should_fall_back
from vinea.gateway.ledger import current_ledger
from vinea.gateway.settings import GatewaySettings, gateway_settings

# LiteLLM's out-of-band metadata. Not in the response body, so not in the SDK's
# typed `ModelResponse` -- which is correct of the SDK and is why the httpx
# client is the seam that reads them.
COST_HEADER = "x-litellm-response-cost"
CACHE_HEADER = "x-litellm-cache-hit"

# (loop_id, settings, model_string, has_direct_key) -> Model. One slot; see docstring.
_cached: tuple[tuple, Model] | None = None


class MeteredModel(WrapperModel):
    """Records `ModelResponse.usage` into the active ledger, then gets out of the way.

    A `WrapperModel` and not an OTel span processor, because the tally has to be
    readable *synchronously* the moment the run ends -- the worker writes it onto
    the advisory row in the same transaction as the advice. Spans are exported
    asynchronously to a collector that may not exist; a row cannot wait for that.

    It records nothing when no `ledger_scope` is open, which is the normal case
    for a CLI run: the metering exists for the batch path, and a wrapper that
    quietly does nothing is better than a batch-only code path the CLI must
    remember to avoid.
    """

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        response = await super().request(messages, model_settings, model_request_parameters)
        ledger = current_ledger()
        if ledger is not None:
            usage = response.usage
            ledger.record_usage(
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                # phase 16: the characters that bought those tokens, counted at
                # the one point in the system that sees the fully-assembled
                # request. Paired here so `context.calibration_ratio` divides two
                # numbers from the SAME call rather than a total by an assumption.
                prompt_chars=_prompt_chars(messages),
                # phase 18: what the provider says served this call, as opposed
                # to the alias we asked for.
                model_name=response.model_name,
            )
        return response


def _prompt_chars(messages: list[ModelMessage]) -> int:
    """Characters in the assembled request, counted best-effort.

    Walks every part of every message and adds the length of whatever carries
    text. Best-effort is the honest description: tool schemas, images and
    provider-side system prompts are not visible here, so this UNDER-counts what
    the provider tokenizes. That direction matters -- a calibration built on it
    reports a chars-per-token ratio lower than the truth, which makes the
    estimator look worse than it is rather than better. Erring toward pessimism
    about our own numbers is the right way round for a budget.
    """
    total = 0
    for message in messages:
        for part in getattr(message, "parts", ()):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                total += sum(len(item) for item in content if isinstance(item, str))
    return total


async def _record_gateway_headers(response: httpx.Response) -> None:
    """httpx response hook: pull cost and cache-hit off the gateway's headers.

    Runs before the body is read, touches only headers, and swallows a malformed
    value rather than raising. An exception here would kill a model call that had
    already succeeded, in order to fail at bookkeeping -- the wrong trade every
    time. A cost we could not parse becomes a NULL, and NULL already means
    "unknown" everywhere else in this schema.
    """
    ledger = current_ledger()
    if ledger is None:
        return

    cost: float | None = None
    raw_cost = response.headers.get(COST_HEADER)
    if raw_cost is not None:
        try:
            cost = float(raw_cost)
        except ValueError:
            cost = None

    cache_hit: bool | None = None
    raw_cache = response.headers.get(CACHE_HEADER)
    if raw_cache is not None:
        cache_hit = raw_cache.strip().lower() in ("true", "1", "yes")

    if cost is not None or cache_hit is not None:
        ledger.record_gateway_headers(cost_usd=cost, cache_hit=cache_hit)


def build_gateway_model(
    settings: GatewaySettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> Model:
    """An `OpenAIChatModel` pointed at LiteLLM, through a client we can listen to.

    The model *name* is the gateway's alias (`vinea-nightly` by default), not a
    provider model id. That indirection is the point of running a gateway at all:
    which upstream serves the alias is the gateway's configuration, and swapping
    it is not a deploy of this repository.

    `transport` is httpx's own injection point, and it is here so the header path
    is testable with no gateway running. It is not a test-only backdoor: the same
    parameter is how you would put a retrying or logging transport underneath, and
    a cost-capture mechanism that can only be verified against a live LiteLLM is a
    mechanism nobody verifies.
    """
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.litellm import LiteLLMProvider

    http_client = httpx.AsyncClient(
        timeout=settings.timeout_seconds,
        event_hooks={"response": [_record_gateway_headers]},
        transport=transport,
    )
    provider = LiteLLMProvider(
        api_key=settings.api_key or "api-key-not-set",
        api_base=settings.url,
        http_client=http_client,
    )
    return OpenAIChatModel(settings.model, provider=provider)


def resolve_model(model: str | None = None) -> str | Model:
    """What `agents.py` passes to `Agent.run(model=...)`.

    Returns a `str` when no gateway is configured. That is deliberate and not a
    typing accident: a string is resolved lazily by pydantic-ai, so an
    `Agent.override(model=TestModel())` never causes a provider to be
    instantiated and the offline suite keeps running with no API key at all.
    Returning an eagerly-built Model here would need a key just to reach the
    line that ignores it.
    """
    global _cached

    model = model or config.MODEL
    settings = gateway_settings()
    if not settings.enabled:
        return model

    # A direct *provider* key decides whether rung 3 exists -- deliberately
    # `has_provider_key` and not `has_api_key`, which now answers True merely
    # because a gateway is configured. Read here rather than at import so a Secret
    # rotation that adds one takes effect on the next run.
    has_direct = config.has_provider_key(model)
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0  # resolved outside a loop; the client binds on first use

    key = (loop_id, settings, model, has_direct)
    if _cached is not None and _cached[0] == key:
        return _cached[1]

    gateway = build_gateway_model(settings)
    if has_direct:
        # `should_fall_back` is what keeps a *budget refusal* from falling through
        # to the direct provider and spending the money the gateway just declined
        # to spend. Reachability failures fall toward the model; policy failures
        # do not.
        resolved: Model = MeteredModel(FallbackModel(gateway, model, fallback_on=should_fall_back))
    else:
        resolved = MeteredModel(gateway)

    _cached = (key, resolved)
    return resolved


def reset_cache() -> None:
    """Drop the memoized model. For tests that change the environment."""
    global _cached
    _cached = None
