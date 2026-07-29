"""phase 14 -- routing, metering, and the two ways a gateway can say no.

Everything here runs offline. The gateway is an `httpx.MockTransport`, which is
not a shortcut: the mechanism under test *is* an HTTP response header, so the
right fake is one that produces real HTTP responses through the real client. A
stub that returned a pre-baked cost object would test nothing, because the claim
being made is precisely that the header reaches the ledger.

There is one live test at the bottom, and it SKIPS unless `VINEA_GATEWAY_URL`
points at something. House rule: anything needing a live service skips with a
reason, never fails red.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models import override_allow_model_requests
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel

from vinea import config
from vinea.gateway import budget
from vinea.gateway.ledger import Ledger, RunCost, ledger_scope
from vinea.gateway.routing import (
    CACHE_HEADER,
    COST_HEADER,
    MeteredModel,
    build_gateway_model,
    reset_cache,
    resolve_model,
)
from vinea.gateway.settings import gateway_settings

_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY")


@pytest.fixture(autouse=True)
def _clean_gateway_env(monkeypatch):
    """No gateway and no provider keys unless a test asks for them.

    The memo in `routing` is keyed by the settings, but it is also keyed by the
    running event loop, and pytest gives each async test a fresh one -- so the
    explicit reset is belt and braces rather than the only thing standing between
    two tests.
    """
    for var in ("VINEA_GATEWAY_URL", "VINEA_GATEWAY_KEY", "VINEA_GATEWAY_MODEL", "VINEA_GATEWAY_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    for var in _PROVIDER_KEYS:
        monkeypatch.delenv(var, raising=False)
    reset_cache()
    yield
    reset_cache()


# --------------------------------------------------------------------------- #
# The ladder                                                                   #
# --------------------------------------------------------------------------- #


def test_no_gateway_returns_the_plain_model_string():
    """The load-bearing one: without a gateway, not even the *type* changes.

    A `str` is resolved lazily by pydantic-ai, so `Agent.override(model=TestModel())`
    never instantiates a provider and the whole offline suite keeps running with no
    API key. Return an eagerly-built Model here and every test in this repository
    needs an ANTHROPIC_API_KEY to reach the line that ignores it.
    """
    resolved = resolve_model()
    assert isinstance(resolved, str)
    assert resolved == config.MODEL


def test_gateway_without_a_provider_key_has_no_failover_rung(monkeypatch):
    """Rung 2. The deployment holds only a virtual key -- which is the point of
    virtual keys -- so there is nothing to fail over to and no FallbackModel."""
    monkeypatch.setenv("VINEA_GATEWAY_URL", "http://gateway:4000")
    monkeypatch.setenv("VINEA_GATEWAY_KEY", "sk-virtual")

    resolved = resolve_model()
    assert isinstance(resolved, MeteredModel)
    assert isinstance(resolved.wrapped, OpenAIChatModel)
    # The alias, not a provider model id: which upstream serves it is the
    # gateway's configuration and not a deploy of this repository.
    assert resolved.model_name == "vinea-nightly"


def test_gateway_with_a_provider_key_adds_the_failover_rung(monkeypatch):
    """Rung 3, and the trade it costs.

    Failover exists only because a provider key is sitting in the same Secret as
    the virtual key -- i.e. the unbounded credential is back in the pod. The
    assertion is really on that coupling: no provider key, no rung (above), and
    the operator chooses which they want.
    """
    monkeypatch.setenv("VINEA_GATEWAY_URL", "http://gateway:4000")
    monkeypatch.setenv("VINEA_GATEWAY_KEY", "sk-virtual")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-used")

    resolved = resolve_model()
    assert isinstance(resolved, MeteredModel)
    assert isinstance(resolved.wrapped, FallbackModel)
    assert len(resolved.wrapped.models) == 2


def test_has_api_key_is_true_behind_a_gateway_with_no_provider_key(monkeypatch):
    """Otherwise every night degrades with a healthy model one hop away.

    `has_provider_key` must stay narrow, though: it is what decides whether the
    failover rung exists, and widening both would silently build a fallback to a
    provider we have no key for.
    """
    monkeypatch.setenv("VINEA_GATEWAY_URL", "http://gateway:4000")
    assert config.has_api_key("anthropic:claude-sonnet-4-5") is True
    assert config.has_provider_key("anthropic:claude-sonnet-4-5") is False


# --------------------------------------------------------------------------- #
# The ledger                                                                   #
# --------------------------------------------------------------------------- #


def test_no_calls_means_four_nulls_not_four_zeros():
    """The router skipped the model, or there was no key. Zero would be a lie:
    it claims a call was made and cost nothing."""
    cost = RunCost.from_ledger(Ledger())
    assert cost == RunCost(input_tokens=None, output_tokens=None, cost_usd=None, cache_hit=None)


def test_tokens_without_a_gateway_still_leave_cost_null():
    """Tokens come from the SDK, cost comes from the gateway. Metering a direct
    provider call therefore yields tokens and an honest NULL for money -- not a
    number reconstructed from a price table we would then have to maintain."""
    ledger = Ledger()
    ledger.record_usage(input_tokens=100, output_tokens=20)
    cost = RunCost.from_ledger(ledger)
    assert (cost.input_tokens, cost.output_tokens) == (100, 20)
    assert cost.cost_usd is None
    assert cost.cache_hit is None


def test_cache_hit_is_all_not_any():
    """The column answers 'did this advisory cost anything new?'.

    An advisory is three model calls. Two cached and one not still bought a
    completion, and an `any()` here would make the cache look better than it is on
    the exact column an operator uses to decide whether to keep paying for it.
    """
    ledger = Ledger()
    for _ in range(3):
        ledger.record_usage(input_tokens=10, output_tokens=5)
    ledger.record_gateway_headers(cost_usd=0.0, cache_hit=True)
    ledger.record_gateway_headers(cost_usd=0.004, cache_hit=False)
    ledger.record_gateway_headers(cost_usd=0.0, cache_hit=True)

    assert RunCost.from_ledger(ledger).cache_hit is False

    all_cached = Ledger()
    all_cached.record_usage(input_tokens=10, output_tokens=5)
    all_cached.record_gateway_headers(cost_usd=0.0, cache_hit=True)
    assert RunCost.from_ledger(all_cached).cache_hit is True


def test_ledger_is_scoped_so_one_task_never_inherits_the_previous_tally():
    with ledger_scope() as first:
        first.record_usage(input_tokens=10, output_tokens=1)
    with ledger_scope() as second:
        assert second.input_tokens == 0


# --------------------------------------------------------------------------- #
# End to end through a fake gateway: does the header actually reach the row?   #
# --------------------------------------------------------------------------- #


def _chat_completion(text: str = "ok") -> dict:
    return {
        "id": "chatcmpl-vinea-test",
        "object": "chat.completion",
        "created": 1769000000,
        "model": "vinea-nightly",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1234, "completion_tokens": 56, "total_tokens": 1290},
    }


def _gateway_agent(*, headers: dict[str, str]) -> Agent:
    """An Agent wired to a gateway that always answers with these headers."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_completion(), headers=headers)

    os.environ.setdefault("VINEA_GATEWAY_URL", "http://gateway:4000")
    settings = gateway_settings()
    model = MeteredModel(build_gateway_model(settings, transport=httpx.MockTransport(handler)))
    return Agent(model=model, output_type=str)


def test_cost_and_cache_headers_reach_the_ledger(monkeypatch):
    """The whole cost mechanism, asserted through a real httpx client.

    LiteLLM reports spend in a response *header*. The SDK's typed `ModelResponse`
    has no room for a vendor's out-of-band metadata -- correctly -- so the seam
    that can read it is the http client we inject, and this test is the proof that
    the seam is wired rather than merely described in a docstring.
    """
    monkeypatch.setenv("VINEA_GATEWAY_URL", "http://gateway:4000")
    agent = _gateway_agent(headers={COST_HEADER: "0.00412", CACHE_HEADER: "false"})

    with ledger_scope() as ledger, override_allow_model_requests(True):
        asyncio.run(agent.run("hello"))

    cost = RunCost.from_ledger(ledger)
    assert cost.input_tokens == 1234        # from the SDK
    assert cost.output_tokens == 56         # from the SDK
    assert cost.cost_usd == pytest.approx(0.00412)   # from the header
    assert cost.cache_hit is False          # from the header


def test_a_gateway_that_reports_no_cost_leaves_the_column_null(monkeypatch):
    """A gateway with cost tracking off, or a direct provider behind the fallback.

    Tokens are still counted, cost stays NULL, and nothing invents a price. This
    is the same rule as `prompt_version` being nullable in phase 1: a column that
    arrives empty is honest, a fabricated default is not.
    """
    monkeypatch.setenv("VINEA_GATEWAY_URL", "http://gateway:4000")
    agent = _gateway_agent(headers={})

    with ledger_scope() as ledger, override_allow_model_requests(True):
        asyncio.run(agent.run("hello"))

    cost = RunCost.from_ledger(ledger)
    assert cost.input_tokens == 1234
    assert cost.cost_usd is None
    assert cost.cache_hit is None


def test_a_malformed_cost_header_does_not_fail_the_advisory(monkeypatch):
    """Bookkeeping must never kill a model call that already succeeded."""
    monkeypatch.setenv("VINEA_GATEWAY_URL", "http://gateway:4000")
    agent = _gateway_agent(headers={COST_HEADER: "not-a-number", CACHE_HEADER: "True"})

    with ledger_scope() as ledger, override_allow_model_requests(True):
        result = asyncio.run(agent.run("hello"))

    assert result.output == "ok"
    cost = RunCost.from_ledger(ledger)
    assert cost.cost_usd is None     # unparseable -> unknown, not zero
    assert cost.cache_hit is True    # the header we could read is still read


# --------------------------------------------------------------------------- #
# Down vs saying-no                                                            #
# --------------------------------------------------------------------------- #


def test_budget_refusal_is_recognised():
    exc = ModelHTTPError(
        status_code=400,
        model_name="vinea-nightly",
        body={"error": {"message": "Budget has been exceeded! Current cost: 12.4, Max budget: 10.0"}},
    )
    assert budget.is_budget_refusal(exc) is True
    assert budget.should_fall_back(exc) is False


def test_an_ordinary_rate_limit_is_not_a_budget_refusal():
    """429 means both things in LiteLLM depending on version, and only the body
    separates them. A rate limit is transient and must be allowed to fall over to
    the direct provider; a budget refusal must not."""
    exc = ModelHTTPError(status_code=429, model_name="vinea-nightly", body={"error": "rate limit exceeded"})
    assert budget.is_budget_refusal(exc) is False
    assert budget.should_fall_back(exc) is True


def test_a_bad_key_is_not_a_budget_refusal():
    """401/403 is 'your key is wrong', a deployment fault. Dressing it up as a
    budget event would send an operator hunting a spend limit that is fine."""
    exc = ModelHTTPError(status_code=401, model_name="vinea-nightly", body={"error": "invalid api key"})
    assert budget.is_budget_refusal(exc) is False


def test_an_outage_falls_over_to_the_direct_provider():
    """Connection refused says nothing about the request; the grower still gets a
    judged advisory and we pay list price for the night."""
    exc = ModelHTTPError(status_code=502, model_name="vinea-nightly", body="bad gateway")
    assert budget.should_fall_back(exc) is True
    assert budget.should_fall_back(httpx.ConnectError("connection refused")) is True


def test_fallback_refuses_to_route_around_a_budget_refusal(monkeypatch):
    """The assertion the whole `budget` module exists for.

    `FallbackModel` re-raises rather than trying the next model when its predicate
    says no. Without that predicate a spend ceiling would be advisory in the worst
    sense: the gateway declines, the direct provider obliges, and the control
    quietly stops controlling the first month it matters.
    """
    monkeypatch.setenv("VINEA_GATEWAY_URL", "http://gateway:4000")
    # Present only so the tripwire model can be *constructed*. Reaching it is the
    # failure this test is looking for.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-be-called")

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "Budget has been exceeded! max_budget: 10.0"}},
            headers={"content-type": "application/json"},
        )

    gateway = build_gateway_model(gateway_settings(), transport=httpx.MockTransport(refuse))
    direct_was_called = False

    class _Tripwire(OpenAIChatModel):
        async def request(self, *args, **kwargs):  # pragma: no cover - must never run
            nonlocal direct_was_called
            direct_was_called = True
            raise AssertionError("the direct provider was reached through a budget refusal")

    fallback = FallbackModel(
        gateway,
        _Tripwire("gpt-4o", provider="openai"),
        fallback_on=budget.should_fall_back,
    )
    agent = Agent(model=fallback, output_type=str)

    with override_allow_model_requests(True), pytest.raises(ModelHTTPError) as caught:
        asyncio.run(agent.run("hello"))

    assert caught.value.status_code == 400
    assert direct_was_called is False


# --------------------------------------------------------------------------- #
# Live gateway -- skips unless one is actually running                         #
# --------------------------------------------------------------------------- #


live_gateway = pytest.mark.skipif(
    not os.getenv("VINEA_GATEWAY_URL"),
    reason=(
        "No VINEA_GATEWAY_URL set. Start one with "
        "`docker compose --profile gateway up -d` to exercise the real proxy."
    ),
)


@live_gateway
def test_live_gateway_reports_its_models():
    """A reachability check, not a model call: it costs nothing and proves the
    alias in `VINEA_GATEWAY_MODEL` is one the gateway actually serves."""
    settings = gateway_settings()
    headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
    response = httpx.get(f"{settings.url}/v1/models", headers=headers, timeout=10)
    response.raise_for_status()
    served = {m["id"] for m in json.loads(response.text)["data"]}
    assert settings.model in served, f"gateway serves {sorted(served)}, not {settings.model!r}"
