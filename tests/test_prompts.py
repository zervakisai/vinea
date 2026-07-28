"""S7.1 / S7.2 / S7.3 -- the prompt registry: the fail-open ladder and the renderer.

All offline. The registry fetch is stubbed (reachable / slow / unreachable) so the
whole ladder is exercised with no live Langfuse -- fresh cache -> fetch -> bundled
default -> NEVER raise, plus stale-while-revalidate.
"""

from __future__ import annotations

import time

import pytest

from vinea.prompts import defaults, registry
from vinea.prompts.cache import PromptCache
from vinea.prompts.registry import TemplateError, render_template


@pytest.fixture(autouse=True)
def _clean_cache():
    registry.reset_cache()
    yield
    registry.reset_cache()


def _stub_fetcher(template: str, *, version: str = "7", delay: float = 0.0, fail: bool = False):
    """Build a fetcher that returns `template`, or is slow, or raises."""

    def fetch(name: str, label: str, *, deadline: float):
        if fail:
            raise RuntimeError("registry unreachable")
        if delay:
            time.sleep(delay)
        return template, version

    return fetch


def _irr_vars() -> dict:
    return {
        "crop": "vineyard", "irrigation_method": "drip",
        "run_date": "2026-07-28", "target_date": "2026-07-29",
        "kc": 0.7, "raw_mm": 67.5, "taw_mm": 150.0,
        "current_depletion_mm": 150.0, "dq_note": "OK",
    }


def _spray_vars() -> dict:
    return {
        "crop": "vineyard", "spray_sensitivity": "high",
        "run_date": "2026-07-28", "target_date": "2026-07-29",
        "deltat_ideal_low": 2.0, "deltat_ideal_high": 8.0,
        "deltat_marginal_upper": 10.0, "deltat_inversion_below": 2.0,
        "wind_ideal_low": 0.83, "wind_ideal_high": 4.2,
        "spray_index_direction": "higher = more suitable", "spray_index_cutoff": 40.0,
        "dq_note": "OK",
    }


# --- the {{variable}} renderer + typed-variable governance (B3-4) ------------


def test_render_substitutes_variables():
    out = render_template("Hello {{crop}}, {{n}}mm", {"crop": "vineyard", "n": "150"})
    assert out == "Hello vineyard, 150mm"


def test_render_tolerates_whitespace_in_placeholders():
    assert render_template("{{ crop }}", {"crop": "olive"}) == "olive"


def test_missing_variable_fails_loudly_not_silently():
    # The governance point: a template that says {{crop}} after the field was renamed
    # to {{crop_name}} must fail at render, not ship a garbled prompt.
    with pytest.raises(TemplateError, match="crop"):
        render_template("Hello {{crop}}", {"crop_name": "vineyard"})


# --- the ladder: fresh cache -> fetch -> bundled default -> never raise ------


def test_registry_reachable_serves_the_live_version(monkeypatch):
    monkeypatch.setattr(
        registry, "_fetcher", lambda: _stub_fetcher("LIVE {{target_date}}", version="9")
    )
    r = registry.render(defaults.IRRIGATION, "production", _irr_vars())
    assert r.source == "registry"
    assert r.version == "9"
    assert r.text == "LIVE 2026-07-29"


def test_second_call_is_served_from_cache(monkeypatch):
    monkeypatch.setattr(registry, "_fetcher", lambda: _stub_fetcher("LIVE {{target_date}}"))
    first = registry.render(defaults.IRRIGATION, "production", _irr_vars())
    second = registry.render(defaults.IRRIGATION, "production", _irr_vars())
    assert first.source == "registry"
    assert second.source == "cache"  # no second fetch


def test_registry_unreachable_falls_back_to_the_bundled_default(monkeypatch):
    monkeypatch.setattr(registry, "_fetcher", lambda: _stub_fetcher("", fail=True))
    r = registry.render(defaults.IRRIGATION, "production", _irr_vars())
    assert r.source == "fallback"
    # The floor renders the shipped default -- a coherent prompt, never a crash.
    assert "Injected config: Kc=" in r.text


def test_render_never_raises_even_when_everything_fails(monkeypatch):
    monkeypatch.setattr(registry, "_fetcher", lambda: _stub_fetcher("", fail=True))
    r = registry.render(defaults.SPRAY, "production", _spray_vars())
    assert r.source == "fallback"
    assert isinstance(r.text, str) and r.text


def test_a_slow_registry_past_the_deadline_is_treated_as_unreachable(monkeypatch):
    def slow_fetcher():
        def fetch(name, label, *, deadline):
            raise TimeoutError("deadline exceeded")

        return fetch

    monkeypatch.setattr(registry, "_fetcher", slow_fetcher)
    r = registry.render(defaults.IRRIGATION, "production", _irr_vars())
    assert r.source == "fallback"


# --- stale-while-revalidate --------------------------------------------------


def test_stale_cache_is_served_immediately_and_revalidated(monkeypatch):
    clock = {"t": 1000.0}
    fast_cache = PromptCache(ttl_seconds=10.0, clock=lambda: clock["t"])
    monkeypatch.setattr(registry, "_cache", fast_cache)

    monkeypatch.setattr(registry, "_fetcher", lambda: _stub_fetcher("V1 {{target_date}}", version="1"))
    r1 = registry.render(defaults.IRRIGATION, "production", _irr_vars())
    assert (r1.source, r1.text) == ("registry", "V1 2026-07-29")

    clock["t"] += 60.0
    monkeypatch.setattr(registry, "_fetcher", lambda: _stub_fetcher("V2 {{target_date}}", version="2"))
    r2 = registry.render(defaults.IRRIGATION, "production", _irr_vars())
    assert r2.source == "cache"
    assert r2.text == "V1 2026-07-29"  # stale served now, not a network wait

    time.sleep(0.2)
    r3 = registry.render(defaults.IRRIGATION, "production", _irr_vars())
    assert r3.text == "V2 2026-07-29"


# --- S7.3: the CI drift check -----------------------------------------------


def test_drift_check_flags_a_production_prompt_that_differs_from_the_floor():
    from vinea.prompts import drift

    def fetcher(name, label, *, deadline):
        return "TOTALLY DIFFERENT production wording", "9"

    results = drift.check_drift(fetcher=fetcher)
    assert all(r.drifted for r in results), "every default differs from this fetcher -> all drift"


def test_drift_check_passes_when_production_matches_the_floor():
    from vinea.prompts import drift

    def fetcher(name, label, *, deadline):
        return defaults.BUNDLED_DEFAULTS[name], "1"  # identical to bundled

    results = drift.check_drift(fetcher=fetcher)
    assert not any(r.drifted for r in results)


def test_a_prompt_not_yet_in_the_registry_is_not_drift():
    from vinea.prompts import drift

    def fetcher(name, label, *, deadline):
        raise RuntimeError("404 not found")

    results = drift.check_drift(fetcher=fetcher)
    assert not any(r.drifted for r in results)
    assert all("not published" in r.detail for r in results)
