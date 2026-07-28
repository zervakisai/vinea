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
