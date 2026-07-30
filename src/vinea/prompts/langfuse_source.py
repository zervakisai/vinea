"""Fetching a prompt template from Langfuse by name@label, over its REST API.

Built on httpx directly rather than the Langfuse SDK, for the same reason the
UI client is: the fail-open ladder (registry.py) needs to own the deadline,
the cache, and the fallback, and a built-in SDK cache would take that control
away. This module is just "GET the template text"; all the policy lives above
it.

`push_prompt` exists so the CI drift check and the demo can seed a
production version, and so a test can create one against a live Langfuse.
Neither is on the request path.
"""

from __future__ import annotations

import os

import httpx


def _base_and_auth() -> tuple[str, tuple[str, str]]:
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "")
    return host, (public, secret)


def fetch_prompt(name: str, label: str, *, deadline: float) -> tuple[str, str | None]:
    """Return (template, version) for name@label, or raise on any problem.

    Raising is correct here -- the ladder in registry.py catches it and falls
    through to the bundled default. `deadline` is the hard timeout that keeps
    the registry off the hot path: exceeded -> raise -> floor.
    """
    host, auth = _base_and_auth()
    if not auth[0] or not auth[1]:
        # No credentials configured -> treat as unreachable, so the ladder
        # uses the bundled default. Telemetry-off-style degrade.
        raise RuntimeError("Langfuse not configured (no LANGFUSE_PUBLIC_KEY/SECRET_KEY)")

    response = httpx.get(
        f"{host}/api/public/v2/prompts/{name}",
        params={"label": label},
        auth=auth,
        timeout=deadline,
    )
    response.raise_for_status()
    body = response.json()
    return body["prompt"], str(body.get("version")) if body.get("version") is not None else None


def push_prompt(name: str, template: str, *, labels: list[str] | None = None) -> dict:
    """Create a new version of a prompt and (optionally) point labels at it.

    Off the request path -- used to seed a production version for the drift
    check and the demo. A new version is immutable; pointing `production` at it
    is how you ship.
    """
    host, auth = _base_and_auth()
    response = httpx.post(
        f"{host}/api/public/v2/prompts",
        json={"name": name, "type": "text", "prompt": template, "labels": labels or ["production"]},
        auth=auth,
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()
