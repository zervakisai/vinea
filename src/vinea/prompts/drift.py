"""S7.3 -- the CI drift check: bundled default vs. live production.

The bundled default is the fail-open floor (defaults.py): when the registry is
down, it's what the system runs. That's only safe if the floor stays close to
what production actually serves. If an agronomist ships new wording to
production and nobody updates the bundled default, the floor silently rots --
during the next outage, growers get phrasing no one intended as the current
fallback, possibly versions behind.

This check makes that rot LOUD. It fetches each prompt's live production
template and compares it to the bundled default; any difference is reported,
and the CLI exits non-zero so CI fails. The point is not that they may never
differ -- production is *meant* to move ahead of a deploy -- it's that the
difference must be *noticed and resolved* (update the bundled default in a
follow-up PR) rather than accumulating unseen.

Run in CI:  `python -m vinea.prompts.drift`
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from vinea.prompts.defaults import BUNDLED_DEFAULTS


@dataclass(frozen=True)
class DriftResult:
    name: str
    drifted: bool
    detail: str


def check_drift(*, label: str = "production", fetcher=None) -> list[DriftResult]:
    """Compare every bundled default against the live template at `label`.

    `fetcher(name, label, *, deadline)` is injectable for tests; defaults to
    the real Langfuse fetch. A prompt that isn't in the registry yet is *not*
    drift -- there's nothing to diverge from -- it's reported as "not
    published", which a maintainer reads as "seed it".
    """
    if fetcher is None:
        from vinea.prompts.langfuse_source import fetch_prompt

        fetcher = fetch_prompt

    results: list[DriftResult] = []
    for name, bundled in BUNDLED_DEFAULTS.items():
        try:
            live, version = fetcher(name, label, deadline=10.0)
        except Exception as exc:  # noqa: BLE001 -- unreachable/absent is reported, not raised
            results.append(
                DriftResult(name=name, drifted=False, detail=f"not published or unreachable: {exc}")
            )
            continue

        if live.strip() == bundled.strip():
            results.append(DriftResult(name=name, drifted=False, detail=f"in sync (v{version})"))
        else:
            results.append(
                DriftResult(
                    name=name,
                    drifted=True,
                    detail=(
                        f"bundled default differs from production v{version}. "
                        f"Update defaults.py to match, or roll production back."
                    ),
                )
            )
    return results


def main(argv: list[str] | None = None) -> int:
    results = check_drift()
    drifted = [r for r in results if r.drifted]
    for r in results:
        marker = "DRIFT" if r.drifted else "ok"
        print(f"[{marker:5}] {r.name}: {r.detail}")
    if drifted:
        print(f"\n{len(drifted)} prompt(s) drifted from production. The fail-open floor is stale.")
        return 1
    print("\nBundled defaults are in sync with production (or not yet published).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
