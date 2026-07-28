# Phase 7 — A second source behind the seam

`git checkout phase-07`

## What you learn

The difference between a seam you *claim* to have and one you can *prove* — and how
to write an adapter that is honest about what its source cannot give you.

## The central idea

[ADR-002](../adr/002-new-source-new-adapter.md): a new data source is a new
adapter, not a rewrite.

`base.py` states the seam as a `Protocol` — structural typing, so `CsvSource` and
`OpenMeteoSource` satisfy it **without inheriting anything**. Both emit
`WeatherRow`s. Past that module, nothing knows or cares whether the numbers came
from a file or from HTTP.

The proof is `tests/test_sources_seam.py`, which runs the same feature pipeline
over both sources and asserts the types out are identical. A seam nobody tests
across is just a naming convention.

## What Open-Meteo gives, and what it doesn't

Most fields map natively: `temperature_2m`, `relative_humidity_2m`,
`wind_speed_10m` (in m/s on request), `precipitation`, `dew_point_2m`,
`vapour_pressure_deficit`, `wind_direction_10m`, `shortwave_radiation`. Notably
`et0_fao_evapotranspiration` is the exact FAO-56 reference ET the water balance
wants — there is no reason to recompute it.

Two fields need care, and they are the interesting ones:

- **`delta_t_c` is reconstructed** as `temperature_2m − wet_bulb_temperature_2m`.
  Delta-T *is* the dry-bulb/wet-bulb depression by definition, so this is an exact
  identity, not an approximation. If either input is missing for an hour, Delta-T
  is `None` for that hour — the same "missing, not guessed" rule as phase 1.
- **`spray_index` is always `None`.** No free source publishes an equivalent
  vendor score. Fabricating one would be exactly the silent-pass the whole system
  refuses.

## Decisions

- **`httpx` directly, not an SDK.** So the fail-open ladder owns the deadline and
  the cache rather than inheriting someone else's retry policy.
- **The CSV loader was left untouched.** `CsvSource` *wraps* `load_weather` instead
  of refactoring it to route through shared code. That is a small duplication
  accepted on purpose: keeping the existing loader's behaviour exact was worth more
  than the DRY win.
- **Tests replay captured responses.** `MockTransport` over
  `tests/fixtures/open_meteo_*.json`, routed by host, so the suite never touches
  the network and no test depends on query-string ordering.

## Read this

- `docs/adr/002-new-source-new-adapter.md`
- `src/vinea/sources/base.py` — the Protocol, ~20 lines
- `src/vinea/sources/open_meteo.py` — the adapter, and a docstring that lists field
  by field what the source does and does not have
- `tests/test_sources_seam.py` — the proof

## The trap

`open_meteo.py`'s docstring used to claim that a missing `spray_index` would take
"the DataQuality 5× penalty for a missing spray-critical cell". **It would not.**

```python
SPRAY_CRITICAL_FIELDS = ("delta_t_c", "wind_ms")     # the index is not in here
```

And `index_ok` fails *open*:

```python
index_ok = (r.spray_index is None or ...)             # None -> True, hour not excluded
```

So the absence costs nothing at the gate and nothing in the 5× term. On the CSV
path it costs nothing at all, because the loader only counts cells among the
columns the file declares, and an absent column is never counted — which is why the
committed capture reports `nan_cells = 0`.

A comment that overstates a guardrail is worse than no comment: it tells the next
reader a check exists where none does. Worth grepping your own docstrings for
claims about penalties, and then checking the constant.

Note also that the seam test's fixtures are captured for Adelaide (−34.75, 138.6)
while the committed CSVs are Nemea. That is fine — the test asserts *type* and
*shape* equality across the two sources, not that they describe the same place —
but it will surprise you the first time you read the coordinates.

## Try it

```bash
uv run vinea --source api --features-only        # live Open-Meteo, needs network
uv run pytest tests/test_sources_seam.py -v      # offline, replayed
```

Compare the two advisories. Then confirm the seam held:

```bash
git diff phase-06 phase-07 -- src/vinea/features.py src/vinea/agents.py src/vinea/graph.py
```

Empty. A whole new data source, and the physics and the agents never learned about
it.
