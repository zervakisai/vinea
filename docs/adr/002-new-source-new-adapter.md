# ADR-002: A new data source is a new adapter behind the same contract

- **Status:** accepted
- **Date:** 2026-07-20
- **Milestone:** phase 7 (live data)

## Context

the core reads two CSVs. Production reads a live feed -- Open-Meteo now, a Bureau
of Meteorology feed later, a grower's own station after that. The question is
where that change is allowed to land.

There is a right answer already implied by the codebase. `WeatherRow` was built
alias-tolerant and `extra="ignore"` *specifically* so that upstream schema drift
lands at the parsing boundary rather than three modules downstream. The water
balance, the spray gates, the agents, and the graph all consume `WeatherRow` and
know nothing about where a row came from. That is a seam whether or not anyone
named it, and phase 7 is where it gets tested under a genuinely different source
rather than a second CSV.

The forcing question: when Open-Meteo turns out not to provide everything a CSV
column did, does that ripple downstream?

## Decision

**A data source is an adapter that emits `WeatherRow`s. Nothing downstream
changes -- not `features.py`, not `agents.py`, not `graph.py`. A new source is a
new module under `sources/` and nothing else.**

Concretely:

- `sources/base.py` states the seam as a `Protocol`: a `WeatherSource` has a
  `load(...) -> WeatherLoadResult`. Structural, not an ABC, so a source qualifies
  by shape rather than by inheritance -- `CsvSource` wraps the the core loader, the
  Open-Meteo source is a class holding an httpx client, and both satisfy it
  without a shared base.
- `ingest.assemble_load_result` is the shared assessment. Everything a
  `DataQuality` measures -- gaps, staleness, forecast reach, missing cells -- is a
  property of the *rows*, so it takes rows and no source, and it computes them
  with the same `_detect_gaps` / `_assess_staleness` / `DataQuality` the CSV
  loader uses. The Open-Meteo adapter fetches and calls it; the CSV loader
  (`load_weather`, the core, deliberately left unchanged) computes the identical
  `DataQuality` shape inline. Same verdict semantics, one set of helper functions.
- The seam test (phase 7) runs the same pipeline over both sources and asserts the
  types out are identical. That test is the ADR made executable.

The mapping's honesty is part of the decision, because a source that lies to fit
the contract breaks everything the contract protects:

- **`et0_fao_evapotranspiration -> et0_mm`.** Native. Open-Meteo publishes
  exactly the FAO-56 reference ET the water balance wants.
- **`delta_t_c` is reconstructed** as `temperature_2m - wet_bulb_temperature_2m`.
  Delta-T *is* the wet-bulb depression by definition, and Open-Meteo publishes
  wet-bulb, so this is an exact identity, not an approximation. Missing either
  input -> `None`, never a guess.
- **`spray_index -> None`, always.** Open-Meteo has no equivalent and it can't be
  reconstructed from physics. The adapter refuses to fabricate one, because a
  fabricated spray index is precisely the silent-pass the whole system is built
  to reject: a `None` spray-critical cell fails its gate closed, and the 5x
  spray-critical penalty lands on it. The source is genuinely weaker for spray
  timing, and the confidence number says so out loud (the adapter attaches a note
  explaining the capped confidence).

## Alternatives considered

**Replace the CSV loader.** Rip out `load_weather`, put HTTP in its place.
Rejected: it throws away the offline, reproducible path that every test and the
demo depend on, and it presumes the API is the only source there will ever be.
The CSV path isn't legacy; it's the golden-dataset path phase 12's eval gate replays
against and the reason `pytest` needs no network.

**Push source-awareness downstream** -- e.g. let `build_features` know whether
data is "live" or "historical" and branch. Rejected, firmly: this is the exact
ripple the seam exists to prevent. The moment `features.py` has an `if source ==
...`, every future source is a change to the deterministic core, and the core is
the thing whose stability the whole design rests on.

**Fabricate the missing spray index** -- interpolate it, or derive a
plausible-looking value from wind and humidity. Rejected as the most dangerous
option on the table. It would make API days *look* complete while silently
inventing a spray-safety input, which is worse than the honest `None` in every
way that matters: the grower would spray on a fabricated number. Missing data
must stay missing and cost confidence; that rule is not negotiable for a
spray-critical field.

**One giant `WeatherSource` ABC with shared HTTP plumbing.** Rejected as
premature: there is one API source today. A `Protocol` states the seam without a
base class that exists only to be inherited from once.

## Consequences

**Good.**

- The seam is now *tested*, not asserted.
  `test_the_same_feature_builder_consumes_both_sources` runs `build_features` over
  Open-Meteo data with the identical call the CSV path uses.
- the core's deterministic core is byte-identical across phase 7: `features.py`,
  `agents.py`, and `graph.py` are untouched. The claim "a new source doesn't move
  features/agents/graph" is a `git diff`, not a hope -- the CLI resolves the
  source in one function (`_resolve_source`) and hands the same
  `(history, forecast, quality)` to the same graph.
- Adding a BoM feed is now a known-size task: one module under `sources/`, one
  branch in `_resolve_source`, done.

**Costs, accepted.**

- Open-Meteo is genuinely worse for spray timing, and the honest `None` makes
  that visible as a capped confidence penalty on every API run. That's not a
  regression to fix; it's the system correctly reporting that this source can't
  fully support a spray decision.
- The archive API is reliable only through yesterday, so `load` fetches history
  ending `today - 1`. A source that needed same-day history would need a
  different endpoint; noted, not solved, because nothing needs it yet.
- `load_weather` and `assemble_load_result` compute `DataQuality` in two places
  rather than one, because refactoring the core's loader to route through the shared
  tail risked changing its missing-cell accounting (it counts only columns the
  CSV actually had). They share the helper functions and the model; keeping the
  the core loader's behaviour exact was worth the small duplication.

## Verification

```bash
# Offline: the seam test, no network.
uv run pytest tests/test_sources_seam.py -q          # 9 passed

# The claim that nothing downstream moved (against the phase 6 tip):
git diff --stat HEAD~ -- src/vinea/features.py src/vinea/agents.py src/vinea/graph.py   # empty

# Live, end to end (needs network):
uv run vinea --source api --features-only
# prints a full deterministic report from live Open-Meteo data, with the note
# that says why the spray leg's confidence is capped.
```
