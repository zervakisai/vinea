"""Is a gateway configured? Everything else in this package branches on the answer.

The default is *no gateway*, and that default is load-bearing: `uv run vinea`
on a laptop with an `ANTHROPIC_API_KEY` must behave exactly as it did in phase
13, with no proxy, no extra hop, and no new failure mode. A gateway appears only
when someone sets `VINEA_GATEWAY_URL`, and it disappears the moment they unset it.

Read from the environment on every call rather than captured at import. That is
not a style preference: the deployment sets these through a Secret, the tests set
them with `monkeypatch`, and a module-level constant frozen at import time would
make the second impossible without reload tricks. `config.MODEL` is a constant
because it was one; this is configuration that a test legitimately toggles.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# The gateway's default alias for the nightly advisory model. It is an alias in
# the gateway's config, NOT a provider model id -- which is the point: swapping
# claude-sonnet for gpt-5 behind `vinea-nightly` is a gateway config change and
# our deployment never learns it happened.
DEFAULT_GATEWAY_MODEL = "vinea-nightly"

# How long a request to the gateway may take before we call it unreachable and
# fail over to the direct provider. Generous, because this is a model call and
# not a registry lookup -- the B3 prompt registry's 0.5s deadline is right for a
# template fetch and would be absurd here.
DEFAULT_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    """Everything needed to talk to the gateway, or `enabled=False`.

    `api_key` is the LiteLLM *virtual key*, not a provider key. That distinction
    is the whole security story of this phase: the deployment holds a key that
    can spend at most what the gateway lets it spend, and the real Anthropic key
    exists only inside the gateway. A leaked virtual key costs its budget; a
    leaked provider key costs whatever the attacker can bear to spend.
    """

    url: str | None = None
    api_key: str | None = None
    model: str = DEFAULT_GATEWAY_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return bool(self.url)


def gateway_settings() -> GatewaySettings:
    """Current gateway configuration from the environment.

    A URL with no key is still `enabled`: LiteLLM can be run with no master key
    for local experiments, and the provider layer supplies a placeholder. It is
    not a configuration we would deploy, but refusing to start on it would turn
    a local convenience into an error message.
    """
    url = os.getenv("VINEA_GATEWAY_URL") or None
    if url is None:
        return GatewaySettings()

    raw_timeout = os.getenv("VINEA_GATEWAY_TIMEOUT")
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        # A typo'd timeout falls back to the default rather than crashing the
        # nightly run. Same instinct as the prompt registry: configuration
        # problems degrade, they do not page.
        timeout = DEFAULT_TIMEOUT_SECONDS

    return GatewaySettings(
        url=url.rstrip("/"),
        api_key=os.getenv("VINEA_GATEWAY_KEY") or None,
        model=os.getenv("VINEA_GATEWAY_MODEL") or DEFAULT_GATEWAY_MODEL,
        timeout_seconds=timeout,
    )
