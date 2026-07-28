# ADR-001: Store what you cannot recompute

- **Status:** accepted
- **Date:** 2026-07-20
- **Milestone:** phase 6 (persistence)

## Context

the core (phases 1–4) holds everything in a `FarmState`/`FarmFeatures` shape for the
duration of one run and nothing after. Adding Postgres forces a question that
never came up before: *what, exactly, is worth a row?*

The tempting answer is "the output" -- persist the advisory and move on. The
tempting second answer is "everything" -- observations, features, advisories,
the lot, because storage is cheap and you never know. Both are wrong in the same
way: they treat persistence as a storage problem rather than a question about
which facts are *recoverable*.

The system already has a sharp line running through it. the core's governing
principle asks "is there a correct numerical answer? yes -> Python, no -> LLM."
The data layer has an equivalent, and it turns out to be the same line seen from
a different angle: **is this value reproducible from inputs we already hold?**

- The water balance is a pure function of `weather_observations` and a `Deps`.
  Given both, `build_features` returns the same depletion today, tomorrow, and in
  five years. It is *derived*.
- What the model said last night is not a function of anything we can re-run. The
  model is non-deterministic, its version moves underneath us, and the prompt
  that produced the text may have been re-pointed since. It is *evidence*.

Those two things want opposite treatment, and conflating them is how a system
ends up unable to answer "why did we tell this grower not to irrigate in March?"

## Decision

**Store what cannot be recomputed. Treat what can be recomputed as cache, and
say so in the schema itself.**

Rows, because they are irreplaceable:

| Table | Why it can't be recomputed |
|---|---|
| `weather_observations` | What the sky did. Gone if we drop it; no API backfills a sensor that wasn't read. |
| `grower_config` | A human chose these thresholds. Versioned, never overwritten. |
| `advisories` | What the model said, plus the provenance that explains it. |
| `eval_runs` | What a score was at a point in time, against tags that have since moved. |
| `annotations` | What a human thought. |

Cache, because it is a pure function of the above:

| Table | Note carried in the schema |
|---|---|
| `feature_cache` | "Reproducible from observations; safe to TRUNCATE." |

Three consequences of the rule, applied consistently:

1. **`raw_mm` is not a column.** It is `mad_fraction * taw_mm`, computed in
   `IrrigationFeatures`. The rule applies inside a single row, not just across
   tables: storing it would give the irrigation trigger a second home and a
   chance to disagree with itself.
2. **`advisories.pre_correction_output` is a column**, even though it looks
   redundant next to the advisory itself. It is the one record of what the model
   said *before* the output validator corrected it, and it is not reconstructible
   from anything else -- see Consequences.
3. **Provenance columns are NOT NULL where they are evidence** (`deps_hash`) and
   nullable where a later stage fills them (`trace_id`, `prompt_version`). A
   column that arrives empty is honest. A fabricated default is not.

## Alternatives considered

**Store the features as truth too.** Superficially attractive -- the nightly run
gets faster, and the UI can read a depletion figure without recomputing. Rejected
because it creates a second source of truth for a number that already has a first
one. The failure mode is specific and nasty: change `effective_rain_fraction`,
and stored features now disagree with what the code computes, silently, for every
historical row. Nothing errors. The eval suite starts scoring against numbers no
current code would produce. Keeping features as an explicitly disposable cache
means that scenario resolves by itself -- you truncate, it recomputes, the numbers
are current by construction.

**Store only the advisories.** Cheapest, and wrong for the reason the
observations table exists: an advisory without the weather that produced it is
unfalsifiable. You cannot re-derive whether the model was right, cannot replay a
golden dataset, and cannot answer an agronomist asking "what did you think the
ET₀ was that week?"

**Store everything, decide later.** This is the option that feels safe and isn't,
because "later" never arrives with better information. The cost isn't disk, it's
that nobody can tell which tables are authoritative. A cache nobody labelled as a
cache eventually gets backed up, then restored, then trusted.

**Event-sourcing the whole thing.** Genuinely a good fit for the append-only half
(observations really are an event log). Rejected on complexity: it earns its place
when you need to reconstruct arbitrary historical state, and here the only state
worth reconstructing is already two tables and a pure function. Complexity must
earn its place.

## Consequences

**Good.**

- `TRUNCATE feature_cache;` is always safe, and the day it isn't, ADR-001 has
  been violated by a specific reader who can be found and fixed. The invariant is
  checkable, not aspirational.
- The five drift tags on `advisories` (`prompt_version`, `model_id`, `deps_hash`,
  `code_sha`, `dataset_version`) make a moved eval score attributable. The case
  DESIGN.md B2 singles out -- the *oracle itself* changing, not the model -- is
  covered because `deps_hash` and `code_sha` are stored on every row.
- `grower_config` being versioned rather than overwritten is what makes an
  advisory from March explicable in July. `deps_hash` is the pointer; the old row
  is the target.
- "New crop = config change, not code change" stops being a claim about `Deps`
  and becomes an `INSERT`.

**Costs, accepted.**

- Recomputing features on every run costs milliseconds we could have saved. Fine:
  the run is overnight and I/O-bound on the model API, and the alternative buys
  speed with a correctness hazard.
- Versioned config makes `save_grower_config` more than an `UPDATE`. It has to
  close the old row and open the new one at a single instant, and that turned out
  to have a real trap in it (Postgres `now()` is transaction time, so two versions
  written in one transaction collide -- the code uses `clock_timestamp()` and says
  why).

**The one worth understanding.** `pre_correction_output` looks like it violates
this ADR -- why store an output we corrected? Because of the circularity trap in
DESIGN.md B2: the oracle plays two roles, and they must not become one code path.
Inline, it's the `output_validator` that forces a retry before a bad number
reaches a grower. Asynchronously, it's the metric an eval scores against. If the
guardrail already fixed the output before anything was logged, the eval scores the
*corrected* answer and reports success on a model that got it wrong every time --
a number that is true about the guardrail and false about the model.

The corrected output is what shipped. The pre-correction output is what the model
actually said, it exists only in memory during the run, and it is gone the moment
the run ends. It is the single least-recomputable value in the system, which is
exactly why it gets a column. The guardrail protects the grower; this column
protects the measurement.

## Verification

```bash
docker compose up -d postgres
uv run alembic upgrade head
psql "$DATABASE_URL" -c "\dt"          # 6 tables + alembic_version
uv run pytest tests/test_db.py -q      # green
```

The ADR's central claim is exercised by `test_raw_mm_is_recomputed_not_stored`:
the irrigation trigger survives a round trip through the database without ever
being a column.
