"""Thin configuration seam: load `.env`, expose the model string and default paths.

Deliberately minimal at phase 1. Real crop parameters (Kc, thresholds, Delta-T/wind bands)
arrive as injected dependencies in phase 2 (#6 — deps_type), NOT as globals here.
"""

from __future__ import annotations

import os
from pathlib import Path

# python-dotenv is a declared dependency; guard anyway so the import never hard-fails.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - defensive only
    pass

# .../src/vinea/config.py -> parents[2] == project root (works for the editable/src layout).
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

DEFAULT_DATA_DIR: Path = Path(os.getenv("VINEA_DATA_DIR", PROJECT_ROOT / "data"))

# Any Pydantic AI model string ("provider:model"). The provider key is read from the env
# by the SDK (ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY). No key lives in code.
# The exact id is finalized when agents are wired in phase 3; phase 1 never calls a model.
MODEL: str = os.getenv("VINEA_MODEL", "anthropic:claude-sonnet-4-5")

# Last-history timestamp older than this (relative to run_date) -> flag data as stale.
STALENESS_THRESHOLD_HOURS: int = 48

# Provider -> env var holding its key. The batch worker (phase 8) reads this to decide
# whether it can run the model at all, or must fall back to the deterministic
# (degraded) advisory. An unknown provider is attempted anyway.
_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google-gla": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}


def has_provider_key(model: str = MODEL) -> bool:
    """True if the provider key for `model` itself is set (or the provider is unknown).

    Phase 1's original `has_api_key`, kept under its own name because phase 14
    needs the narrow question as well as the wide one. The gateway's failover rung
    can only exist if the deployment holds a *provider* key next to the virtual
    key -- and the whole point of virtual keys is that it usually should not.
    """
    env = _KEY_ENV.get(model.split(":", 1)[0])
    return env is None or bool(os.getenv(env))


def has_api_key(model: str = MODEL) -> bool:
    """True if a model call is possible at all: via a gateway key, or a provider key.

    phase 14 widened the question this answers. A gateway deployment holds a
    LiteLLM *virtual* key and, by design, may hold no provider key whatsoever --
    that is the security benefit of running one. Asking for ANTHROPIC_API_KEY
    there would send every night down the deterministic degrade with a perfectly
    healthy model one hop away.

    The env var is read directly rather than through `gateway.settings` because
    `gateway.routing` imports this module; the duplication is one string, and the
    import cycle it avoids is not worth a lazy import to hide.
    """
    return bool(os.getenv("VINEA_GATEWAY_URL")) or has_provider_key(model)
