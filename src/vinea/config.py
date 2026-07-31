"""Thin configuration seam: load `.env`, expose the model string and default paths.

Deliberately minimal. Real crop parameters (Kc, thresholds, Delta-T/wind bands) are
injected dependencies (`deps.Deps`), never globals here.
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

# `VINEA_DATA_DIR` is authoritative; the fallback only works for a source checkout.
#
# PROJECT_ROOT above is `parents[2]`, which is the repository root when this file
# lives at `src/vinea/config.py` and is `.venv/lib/pythonX.Y` when the package is
# installed as a wheel -- so an installed deployment MUST set VINEA_DATA_DIR. The
# Dockerfile does. A packaging that forgets gets a path that does not exist, and
# `sorted(dir.glob(...))[-1]` raises IndexError somewhere far from the cause.
DEFAULT_DATA_DIR: Path = Path(os.getenv("VINEA_DATA_DIR", PROJECT_ROOT / "data"))

# Any Pydantic AI model string ("provider:model"). The provider key is read from the env
# by the SDK (ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY). No key lives in code.
# Importing this module never calls a model; the provider is built at run time.
MODEL: str = os.getenv("VINEA_MODEL", "anthropic:claude-sonnet-4-5")

# Last-history timestamp older than this (relative to run_date) -> flag data as stale.
STALENESS_THRESHOLD_HOURS: int = 48

# Provider -> env var holding its key. The batch worker reads this to decide
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

    The narrow question, kept under its own name because both are needed. The gateway's failover rung
    can only exist if the deployment holds a *provider* key next to the virtual
    key -- and the whole point of virtual keys is that it usually should not.
    """
    env = _KEY_ENV.get(model.split(":", 1)[0])
    return env is None or bool(os.getenv(env))


def has_api_key(model: str = MODEL) -> bool:
    """True if a model call is possible at all: via a gateway key, or a provider key.

    Wider than `has_provider_key`. A gateway deployment holds a
    LiteLLM *virtual* key and, by design, may hold no provider key whatsoever --
    that is the security benefit of running one. Asking for ANTHROPIC_API_KEY
    there would send every night down the deterministic degrade with a perfectly
    healthy model one hop away.

    The env var is read directly rather than through `gateway.settings` because
    `gateway.routing` imports this module; the duplication is one string, and the
    import cycle it avoids is not worth a lazy import to hide.
    """
    return bool(os.getenv("VINEA_GATEWAY_URL")) or has_provider_key(model)
