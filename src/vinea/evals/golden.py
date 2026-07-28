"""S7.5 -- golden replay and the five drift tags.

DESIGN.md B2: drift is two questions kept separate -- did the *model* change, or did
the *input distribution* change? A golden replay answers the first by holding the
second constant: the same frozen inputs (this repo's `data/` fixtures) run against
whatever's currently deployed. If the score moves and the inputs didn't, the model
(or prompt, or oracle) did.

Every eval run is tagged with the five things that could have moved a score:

  prompt_version, model_id, deps_hash, code_sha, dataset_version

so when a score moves, the tags say which of the five moved with it -- including the
case B2 singles out, where nothing about the model changed and the *oracle itself*
did (an intentional constant change like `effective_rain_fraction`, which should
visibly move scores and be traceable to that change, not mistaken for model drift).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from vinea.db.mapping import deps_hash
from vinea.deps import Deps
from vinea.ingest import WeatherLoadResult, load_weather

# Bump when the fixtures change. It's one of the five tags; a moved score after a
# dataset bump is attributable to the data, not the model.
DATASET_VERSION = "fixtures-v1"

# The frozen fixtures are dated 2026-06-24; this is the run_date the golden replay
# advises about (so target_date = 2026-07-29, inside the forecast window).
GOLDEN_RUN_DATE = date(2026, 7, 28)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DATA = _REPO_ROOT / "data"


@dataclass(frozen=True)
class DriftTags:
    """The five tags that make a moved eval score attributable (B2)."""

    prompt_version: str
    model_id: str
    deps_hash: str
    code_sha: str
    dataset_version: str


def code_sha() -> str:
    """The current git SHA, or 'unknown' outside a repo. One of the five tags."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()[:12]
    except Exception:  # noqa: BLE001 -- no git is fine, the tag just says so
        return "unknown"


def drift_tags(
    deps: Deps, *, prompt_version: str, model_id: str, dataset_version: str = DATASET_VERSION
) -> DriftTags:
    """Assemble the five tags for an eval run. `deps_hash` and `code_sha` computed
    here so the two B2 calls out -- oracle change, code change -- are always
    captured."""
    return DriftTags(
        prompt_version=prompt_version,
        model_id=model_id,
        deps_hash=deps_hash(deps),
        code_sha=code_sha(),
        dataset_version=dataset_version,
    )


def _find(marker: str) -> Path:
    return sorted(_DATA.glob(f"*{marker}*.csv"))[-1]


def load_golden() -> WeatherLoadResult:
    """The frozen golden inputs -- the same fixtures the core hand-checks.

    Held constant on purpose: a golden replay varies everything *except* the inputs,
    so any score movement is the model/prompt/oracle, never the data.
    """
    history, forecast, quality = load_weather(_find("last-30d"), _find("next-7d"), GOLDEN_RUN_DATE)
    return WeatherLoadResult(history=history, forecast=forecast, quality=quality)
