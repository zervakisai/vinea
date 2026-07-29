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

---

# The retrieval corpus (phase 15)

`corpus/fao56-chunks.jsonl` — 798 passages of **FAO Irrigation and Drainage Paper
56, *Crop evapotranspiration*, second edition revised 2025**.

## Source

FAO. 2025. *Crop evapotranspiration — Guidelines for computing crop water
requirements. Second edition, revised 2025.* FAO Irrigation and Drainage Paper
No. 56 Rev.1. Rome, FAO. <https://doi.org/10.4060/cd6621en>

Licence: **Creative Commons Attribution 4.0 International (CC BY 4.0)** — the
same licence as the weather data above, which keeps this file one story rather
than two.

That licence is not taken on trust. `scripts/fetch_corpus.py` resolves the item
through FAO's repository API and asserts `dc.rights.license == "CC BY 4.0"`
before writing a single byte; if FAO ever relicenses, regeneration fails loudly
instead of quietly redistributing something we no longer may. The same record is
also written as the first line of the JSONL, so a copy of the corpus separated
from this file still carries its own provenance.

## Why this document

It is the document `src/vinea/features.py` implements. The FAO-56 water balance,
`ETc = ETo x Kc`, `RAW = p x TAW` and the depletion recurrence are all from here.
Grounding an FAO-56 implementation in FAO-56 is the point — and it is also the
hazard the phase is built around: **this corpus contains the constants the code
computes with, and nothing retrieved from it is allowed to reach a computation.**
See `docs/phases/15-rag-and-citations.md`.

## What was changed

FAO publishes an extracted plain-text rendering (1.05 MB) beside the 22 MB PDF.
That extraction is used as-is — there is no PDF parser in this project, and no
chance of our extraction differing from theirs. Three transformations are applied
to it, all of them removals or annotations, never edits to the text:

| | |
|---|---|
| Front matter dropped | everything above the first `Chapter N` marker: cover, table of contents, licence page, publisher disclaimer |
| Non-prose lines dropped | flattened equations, figure axis labels and page furniture, by the `_is_prose` heuristic in the fetch script |
| Bibliography entries dropped | lines matching `Author, X.` — they retrieve well and say nothing |
| Chunked | ~1200 characters with 200 of overlap, split at chapter and Table/Figure/Box/Example boundaries |
| Annotated | each chunk carries `chapter`, `section` and a `locator`, so a citation names a place a reader can go and check |

No word of the retained text is altered. Chunk boundaries and dropped lines are
reproducible by re-running the script.

## Regenerating

```bash
uv run python scripts/fetch_corpus.py --dry-run   # summary only
uv run python scripts/fetch_corpus.py             # rewrite data/corpus/
python -m vinea.rag ingest                        # embed into Postgres
```
