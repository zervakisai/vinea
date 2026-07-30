"""Prompt management: templates in a registry, numbers in deps.

The instruction *framings* in `agents.py` (the `render_*_context`
functions) are product copy owned by agronomists, who iterate on wording faster
than anyone wants to cut a deploy. So the templates live outside the deploy
artifact, in a registry (self-hosted Langfuse, ADR-004), fetched by `name@label`
-- label a mutable pointer ("production"), version immutable. Shipping a prompt is
saving a version and re-pointing the label; rolling back is re-pointing it.

The property that makes a registry outage strictly bounded: **the registry holds
templates only.** The day's config and computed depletion are substituted into the
template *locally*, at request time, from the agent's deps, and never reach the
registry. An outage degrades *wording* (yesterday's phrasing instead of today's);
it cannot degrade the *decision*, because the decision was never the registry's to
hold.

  defaults.py   the bundled default templates -- the fail-open floor, shipped in
                the artifact, identical to the f-strings they replaced.
  cache.py      the in-process SWR cache that keeps the registry off the hot path.
  registry.py   the fetch-by-name@label ladder (fresh cache -> fetch with a
                deadline -> bundled default -> NEVER raise) and the `{{variable}}`
                renderer with typed-variable checking.
"""

from vinea.prompts.registry import RenderedPrompt, render

__all__ = ["RenderedPrompt", "render"]
