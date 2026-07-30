# Phase 12 — Prompt registry & the eval gate

`git checkout phase-12`

## What you learn

Two things that only work because of a decision made in phase 2: how to let
non-engineers change prompts without a deploy *and* without being able to change a
decision, and how to score an LLM against **ground truth** instead of another LLM's
opinion.

## Part one — prompts without redeploying

The instruction framings are **product copy owned by agronomists**, who iterate on
wording far faster than anyone wants to cut a deploy. So the templates live outside
the deploy artifact, in a registry, fetched by `name@label` — label a mutable
pointer (`production`), version immutable. Shipping a prompt is saving a version and
re-pointing the label. Rolling back is re-pointing it again.

**Why `name@label` and not a version number:** a pinned version means shipping a
prompt requires a code change to bump the pin, which is the thing you were trying to
avoid.

The ladder, on every request, in strict order:

```
fresh cache  →  fetch with a deadline  →  bundled default  →  NEVER raise
```

`defaults.py` is the fail-open floor, shipped in the artifact, identical to the
f-strings it replaced. `cache.py` is stale-while-revalidate, so the registry stays
off the hot path.

**The property that bounds the outage:** the registry holds **templates only**. The
day's config and the computed depletion are substituted locally, at request time,
from the agent's deps — they never reach the registry. So a registry outage degrades
*wording* (yesterday's phrasing instead of today's). It cannot degrade the
*decision*, because the decision was never the registry's to hold.

That is phase 2 paying off again: numbers live in `deps`, so the thing you
externalised is only prose.

## Part two — the eval gate

For legs with a **correct numerical answer**, the scorer must not be an LLM judging
an LLM. It is the same deterministic functions `features.py` already has, wrapped
for scoring — `oracles.py` wraps them rather than reimplementing, so the eval and
the product share one water balance.

- **`asymmetric.py`** — a missed irrigation costs ≈ **5×** an unnecessary one, and
  the score is **recall-gated**. Unequal errors deserve unequal costs: a vine that
  needed water and did not get it is not the same mistake as a wasted irrigation
  cycle.
- **`golden.py`** — golden replay over the frozen capture, tagged with the **five
  drift tags**: `prompt_version`, `model_id`, `deps_hash`, `code_sha`,
  `dataset_version`. When a score moves, the tags say which of the five moved with
  it — including the case where nothing about the model changed and the *oracle*
  did.
- **`judge.py`** — LLM-as-judge, confined to the one artifact with no ground truth:
  the grower-facing `summary`. It is given the plan text and **not** the depletion
  number, so it cannot grade the arithmetic. A different, stronger judge model, with
  an explicit rubric.

## The circularity trap, and where it is handled

The oracle plays two roles that must **not** become one code path:

| | Role | Scores |
|---|---|---|
| Inline | `output_validator` raising `ModelRetry` | protects the live request |
| Async | the eval metric | measures the model |

If the async eval scored the *shipped* output, it would be scoring output the inline
guard had already corrected — a perfect score, forever, guaranteed by construction.
So it scores `advisories.pre_correction_output`, captured in phase 9. Guardrail
protects the grower; eval measures the model.

## Read this

- `src/vinea/prompts/registry.py` — the fail-open ladder
- `src/vinea/prompts/drift.py` — CI check: bundled floor vs `@production`
- `src/vinea/evals/oracles.py` — wrapping, not reimplementing
- `src/vinea/evals/asymmetric.py` — the 5× and the recall gate
- `src/vinea/evals/golden.py` — the five drift tags

## The trap

The **bundled floor drifts silently**. `defaults.py` starts identical to what the
registry serves, and then agronomists improve the registry copy for six months. The
fallback still works — it never raises, which is the whole design — so nothing ever
tells you that your fail-open path now serves prose from last spring.

`prompts/drift.py` exists for exactly this and runs in CI. But note what it can do:
it compares the bundled default against `@production` and reports divergence. It
cannot tell you whether the divergence *matters*, and it is a **no-op without
Langfuse credentials** — an unpublished prompt is "not published", not drift. On a
fresh clone with no secrets set, the check passes by doing nothing.

So the guardrail is real in CI with secrets, and decorative without them. Know which
one you are running.

## Try it

```bash
uv run python -m vinea.prompts.drift        # no-op without LANGFUSE_* set
uv run pytest tests/test_prompts.py tests/test_evals.py -v
```

Then find the eval test that feeds a deliberately wrong depletion (100.0 mm against
a true 133.5 mm) and check the reported error: **33.5 mm**, and
`depletion_within_tolerance` is `False`. That number is computed by the same
function that produced the advisory — which is the point, and also why
`dataset_version` has to be one of the five tags.
