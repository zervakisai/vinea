# Data provenance & attribution

The two CSVs in this directory are **committed on purpose**. The test suite runs
fully offline and the numbers quoted in the README are checkable, and neither is
true if the data has to be fetched first.

## Source

Weather data by [Open-Meteo.com](https://open-meteo.com/), used under the
**Creative Commons Attribution 4.0 International (CC BY 4.0)** licence.

Open-Meteo aggregates several national weather services, each with its own
licence (mostly CC-BY variants); the API output itself is offered as CC BY 4.0.
No modification has been made beyond the two documented below.

## Capture

| | |
|---|---|
| Site | Nemea, Corinthia, Greece — 37.8125° N, 22.6875° E, 326 m |
| Timezone | `Europe/Athens` (timestamps are tz-naive, site-local) |
| Endpoint | `https://api.open-meteo.com/v1/forecast` (`past_days` + `forecast_days`) |
| Captured | 2026-07-28 |
| History | `nemea_weather_last-30d_1h_2026-07-28.csv` — 720 rows, 2026-06-28T00:00 → 2026-07-27T23:00 |
| Forecast | `nemea_weather_next-7d_1h_2026-07-28.csv` — 168 rows, 2026-07-28T00:00 → 2026-08-03T23:00 |
| Missing cells | none |

Both windows come from **one** call to the forecast endpoint. The archive
endpoint (ERA5 reanalysis) lags several days and so cannot reach yesterday;
splicing reanalysis onto forecast would put a model discontinuity in the middle
of the history, which the running water balance would integrate straight over.
One model, one continuous series.

## The two derived columns

Everything else is passed through as returned. Two columns are not:

- **`Snowfall (mm/h)`** — Open-Meteo reports snowfall in **cm**; the schema is
  mm, so values are multiplied by 10. A silent unit mismatch is the classic way
  a committed dataset lies.
- **`Delta T (°C)`** — not published directly. Delta-T *is* the dry-bulb/wet-bulb
  depression by definition, and Open-Meteo publishes `wet_bulb_temperature_2m`,
  so this is `temperature_2m − wet_bulb_temperature_2m` — an exact identity, not
  an approximation.

## No vendor spray index

`WeatherRow` carries an optional `spray_index` — an externally supplied 0–100
suitability score. No free weather source publishes an equivalent, so this
dataset has no such column and the field parses as `None` for every row.

That is deliberate, and worth understanding rather than working around:

- The spray gate **fails open** on a missing index (`index_ok` is `True` when
  `spray_index is None`), so no window is lost to the absence.
- `SPRAY_CRITICAL_FIELDS` is `("delta_t_c", "wind_ms")` — the index is *not*
  spray-critical, so its absence carries no 5× penalty.
- The CSV loader counts missing cells among the columns the file actually
  declares, so an absent column is not counted at all: `nan_cells` is 0 and
  `confidence_penalty` is 0.0 for this capture.

The three gates that remain — Delta-T bands, wind bands, rain-fastness — are all
physics, and all explainable. Nothing in the advisory depends on an opaque score.

## Regenerating

```bash
uv run python scripts/fetch_dataset.py --dry-run   # fetch and report, write nothing
uv run python scripts/fetch_dataset.py             # rewrite data/ in place
```

**The suite is pinned to this capture.** `GOLDEN_RUN_DATE` in
`src/vinea/evals/golden.py`, the `RUN_DATE`/`TOMORROW` constants in the tests,
and the hand-checked numbers (137.98 mm ETc, 133.5 mm depletion, 25.70 °C first
row) all describe the files above. Regenerating the data means rebaselining
those — which is the honest cost of committing real numbers instead of synthetic
ones, and is why `DATASET_VERSION` is one of the five drift tags.
