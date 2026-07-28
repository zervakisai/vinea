"""The fetch-by-name@label ladder, the renderer, and the prompt.source tag.

`render(name, label, variables)` is the whole public surface. It returns the
fully-rendered instructions string plus where the template came from, and it
**never raises** for a registry problem -- the fail-open floor guarantees a
coherent prompt no matter what.

The ladder (DESIGN.md B3, S7.2), in order:

  1. Fresh cache      -> serve it.                          source = "cache"
  2. Stale cache      -> serve it, revalidate in background. source = "cache"
  3. No cache         -> fetch with a deadline;
                           success -> cache + serve.         source = "registry"
                           timeout/unreachable -> step 4.
  4. Bundled default  -> the floor. Always works.            source = "fallback"

The `source` is stamped on the current span as `prompt.source`, so a trace
shows whether an advisory's wording came from the live registry, a cached
copy, or the shipped floor -- attributability for the phrasing, the way the
five drift tags are attributability for the decision.

Templating is `{{variable}}` (Langfuse's syntax, so the same template renders
here and there). The renderer is *strict on missing variables*: a placeholder
with no matching variable raises `TemplateError` -- the typed-variable
governance point (B3-4). A template that still says `{{crop}}` after the field
was renamed to `{{crop_name}}` fails at render with a clear message, not three
garbled words into a prompt in front of a grower.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass

from opentelemetry import trace

from vinea.prompts.cache import PromptCache
from vinea.prompts.defaults import BUNDLED_DEFAULTS

# The deadline a single fetch may take before we fall through to the floor.
# Short on purpose: the registry is never allowed to add real latency to a
# grower's request (B3). Missed -> bundled default, next request tries again.
FETCH_DEADLINE_SECONDS = 0.5

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# One process-wide cache and one lock guarding in-flight background refreshes.
_cache = PromptCache(ttl_seconds=float(os.environ.get("VINEA_PROMPT_TTL", "60")))
_revalidating: set[str] = set()
_revalidate_lock = threading.Lock()


class TemplateError(ValueError):
    """A template referenced a variable that wasn't provided (B3-4)."""


@dataclass(frozen=True)
class RenderedPrompt:
    text: str
    source: str  # "registry" | "cache" | "fallback"
    version: str | None


def _fetcher():
    """The function that actually fetches a template from Langfuse.

    Indirected through a module global so tests can swap it for a stub
    (reachable / slow / unreachable) with no live registry, and so the whole
    ladder is exercisable offline. Returns (template, version) or raises.
    """
    from vinea.prompts.langfuse_source import fetch_prompt

    return fetch_prompt


def render_template(template: str, variables: dict[str, str]) -> str:
    """Substitute `{{var}}`, strictly. Missing variable -> TemplateError.

    Strictness is the governance feature, not a nuisance: an unfilled
    placeholder means the template and the caller disagree about the contract,
    and that should fail loudly at render, not silently ship `{{crop}}` to a
    model.
    """
    missing: list[str] = []

    def _sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in variables:
            missing.append(name)
            return match.group(0)
        return str(variables[name])

    result = _PLACEHOLDER.sub(_sub, template)
    if missing:
        raise TemplateError(
            f"template references undefined variable(s): {sorted(set(missing))}. "
            f"provided: {sorted(variables)}"
        )
    return result


def _cache_key(name: str, label: str) -> str:
    return f"{name}@{label}"


def _revalidate_in_background(name: str, label: str, key: str) -> None:
    """Refresh a stale cache entry off the request path (the WR in SWR)."""
    with _revalidate_lock:
        if key in _revalidating:
            return  # a refresh is already in flight; don't stack them
        _revalidating.add(key)

    def _work() -> None:
        try:
            template, version = _fetcher()(name, label, deadline=FETCH_DEADLINE_SECONDS)
            _cache.put(key, template, version)
        except Exception:  # noqa: BLE001 -- a failed revalidation just keeps the stale entry
            pass
        finally:
            with _revalidate_lock:
                _revalidating.discard(key)

    threading.Thread(target=_work, daemon=True).start()


def _resolve(name: str, label: str) -> tuple[str, str, str | None]:
    """Walk the ladder, return (template, source, version). Never raises."""
    key = _cache_key(name, label)
    entry = _cache.get(key)

    if entry is not None:
        if _cache.is_fresh(entry):
            return entry.template, "cache", entry.version
        # Stale: serve it now, refresh in the background.
        _revalidate_in_background(name, label, key)
        return entry.template, "cache", entry.version

    # Cold cache: fetch with a deadline.
    try:
        template, version = _fetcher()(name, label, deadline=FETCH_DEADLINE_SECONDS)
        _cache.put(key, template, version)
        return template, "registry", version
    except Exception:  # noqa: BLE001 -- registry down/slow -> the floor, never a crash
        return BUNDLED_DEFAULTS[name], "fallback", None


def render(name: str, label: str, variables: dict[str, str]) -> RenderedPrompt:
    """Resolve a template by name@label and render it with today's variables.

    The template comes from the ladder (cache/registry/bundled); the variables
    are the day's numbers, substituted here, locally -- they never went to the
    registry. `prompt.source` is stamped on the current span.
    """
    template, source, version = _resolve(name, label)
    text = render_template(template, variables)

    span = trace.get_current_span()
    if span.get_span_context().is_valid:
        span.set_attribute("prompt.name", name)
        span.set_attribute("prompt.label", label)
        span.set_attribute("prompt.source", source)
        if version is not None:
            span.set_attribute("prompt.version", str(version))

    return RenderedPrompt(text=text, source=source, version=version)


def reset_cache() -> None:
    """Clear the cache (tests, and a manual refresh)."""
    _cache.clear()
