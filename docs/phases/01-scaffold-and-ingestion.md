# Phase 1 — Scaffold & ingestion

`git checkout phase-01`

## What you learn

How to make untrusted tabular input safe *once*, at the edge, so that nothing
downstream has to be defensive — and how to record what was wrong with the data
as **data**, rather than logging it and moving on.

## The central idea

There are two honest ways to handle a corrupt cell in a weather feed: refuse to
run, or carry the damage forward in a form the rest of the system can reason
about. Almost every real system needs the second. The one thing you must not do
is the tempting third option: substitute a plausible number.

A `NaN` in an ET₀ column that becomes `0.0` does not "degrade gracefully". It
silently tells the water balance that the vine used no water that hour, which
lowers depletion, which turns the irrigation trigger off. Nobody sees an error.
The grower doesn't irrigate. That is the failure mode this phase exists to close.

So: every numeric cell is coerced to `float` or to `None`. Never to zero.

## Decisions

- **Unicode headers are mapped by Pydantic `alias`, in one place.** The CSV says
  `Reference ET₀ (mm)` with a U+2080 subscript zero; the model says `et0_mm`. The
  alias on the field is the *single* source of truth for that rename, so there is
  no dictionary of header strings drifting somewhere else.
- **Missing/non-finite → `None`, counted.** Empty, `NaN`, `inf` and `-inf` all
  become `None`, and the loader counts them: `nan_cells`, and separately
  `spray_critical_nan_cells` for the two fields where absence is worse
  (`delta_t_c`, `wind_ms`).
- **Data quality is a return value, not a log line.** `load_weather` returns
  `(history, forecast, DataQuality)`. Because it is a value, phase 4 can turn it
  into a confidence ceiling, phase 6 can persist it, and phase 9 can put it on a
  span. A log line can do none of those.
- **Timestamps are tz-naive and site-local.** The CSV carries no offset, so
  parsing produces naive `datetime`, and they are treated as the same wall clock
  as `run_date`. UTC normalisation is deliberately deferred — mixing a UTC series
  with a local `run_date` would quietly skew `staleness_hours`.
- **The data path is a parameter.** `--data-dir`, `--history`, `--forecast`,
  `VINEA_DATA_DIR`. Data location never becomes a fact baked into logic.

## Read this

- `src/vinea/ingest.py` — `WeatherRow`, the coercion, `DataQuality`
- `src/vinea/config.py` — the config seam: env, paths, model string
- `tests/fixtures/` — five tiny CSVs, one per way data goes wrong

## The trap

`DataQuality.confidence_penalty` is a **capped sum**, and the caps matter:

```python
if self.nan_cells:
    p += min(0.15, 0.01 * self.nan_cells)
```

Without the cap, a feed with 200 bad cells produces a penalty of 2.0 and the
advisory's confidence goes negative. With it, "quite a lot of missing data" and
"almost all missing data" both land at 0.15 — which is a real limitation, not a
bug: past a point the penalty stops discriminating, and you should be rejecting
the feed rather than scoring it. The cap keeps the number meaningful; noticing
that it saturates is what tells you when to stop trusting the mechanism.

Note also what is *not* counted. The loader counts missing cells among the
columns the CSV actually declares, so a column that is absent entirely costs
nothing. That is why the committed dataset — which has no vendor spray index
column — reports `nan_cells = 0` and a penalty of `0.0`.

## Try it

```bash
uv run vinea --features-only
uv run pytest tests/test_ingest.py -v
```

Then break it on purpose. Add a row to the history CSV with `not-a-timestamp` in
the first column, and re-run: the row is dropped, `rows_dropped` becomes 1, and a
note says so — the advisory still comes out. Now change an ET₀ value to `NaN` and
watch `nan_cells` move but the depletion stay a *lower* bound rather than
dropping. That asymmetry is the whole point of the phase.
