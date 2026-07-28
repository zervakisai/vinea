# Phase 4 — Robustness

`git checkout phase-04`

## What you learn

How to make degraded input produce *degraded confidence* rather than a crash or a
confident wrong answer — and how to test an LLM system offline, deterministically,
with no API key and no network.

## The central idea

Phase 1 measured what was wrong with the data. This phase spends that measurement.

`DataQuality.confidence_penalty` becomes a **ceiling**:

```python
ceiling = round(1.0 - dq.confidence_penalty, 3)
confidence = min(model_confidence, ceiling)
```

The model is free to be as confident as it likes; it cannot exceed what the
evidence supports. And the reason is surfaced — the notes travel into the
rationale as a caveat, so a grower reading "confidence 0.6" can see *why*.

This is the payoff for having made data quality a return value in phase 1 instead
of a log line. You cannot clamp against a log line.

## Decisions

- **Clamp, don't reject.** A stale feed still produces advice, at lower
  confidence, with the staleness stated. Refusing to answer is worse for a grower
  than answering with a stated caveat.
- **Degrade to deterministic, don't crash.** If the model exhausts `retries=2`,
  the CLI falls back to the deterministic features. There is always output.
- **No key? Still runs.** With no provider key set, `uv run vinea` automatically
  behaves as `--features-only`. The one-command promise in the README holds for
  someone who just cloned the repo.
- **Offline tests, enforced not requested.** `conftest.py` sets
  `pydantic_ai.models.ALLOW_MODEL_REQUESTS = False` at import. Agents are driven
  by `TestModel` / `FunctionModel` through `Agent.override`. A test that
  accidentally reaches for a live model fails loudly instead of billing someone.

## Read this

- `src/vinea/agents.py` — `_clamp_confidence` and the caveat plumbing
- `tests/conftest.py` — the backstop
- `tests/test_robustness.py` — bad input, end to end
- `tests/fixtures/` — one fixture per failure mode: NaN, gap, bad date, stale

## The trap

The penalty **saturates**, and the caps are not incidental:

```python
p = 0.0
if self.rows_dropped:              p += min(0.20, 0.02 * self.rows_dropped)
if self.gap_count:                 p += min(0.20, 0.01 * self.gap_count)
if self.nan_cells:                 p += min(0.15, 0.01 * self.nan_cells)
if self.spray_critical_nan_cells:  p += min(0.20, 0.05 * self.spray_critical_nan_cells)
if self.is_stale:                  p += 0.30
if not self.forecast_covers_tomorrow: p += 0.30
return round(min(p, 0.9), 3)
```

Fifteen missing cells and fifteen hundred both yield 0.15. The overall cap of 0.9
means confidence never reaches zero however broken the input is. So the ceiling is
a *sensitivity* mechanism for ordinary degradation, not an admissibility test for
garbage. Somewhere above these caps is a feed you should reject outright, and this
number will not tell you where that line is — it will just quietly bottom out at
0.1 confidence and keep advising.

Worth knowing about your own guardrail before you rely on it.

## Try it

```bash
uv run pytest -q                   # 115 passed, 65 skipped
uv run pytest tests/test_robustness.py -v
```

The committed capture is clean and fresh, so `confidence_penalty` is `0.0` — the
mechanism is invisible on the happy path, which is exactly why the fixtures exist.
Point the CLI at a stale fixture and watch the ceiling bite:

```bash
uv run vinea --features-only \
  --history tests/fixtures/fixture_stale.csv \
  --forecast tests/fixtures/fixture_clean.csv
```

Then read the notes it prints. Every one of them is a fact the loader recorded in
phase 1 and this phase turned into a number a grower can act on.
