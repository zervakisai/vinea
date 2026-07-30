# Phase 2 — The deterministic core

`git checkout phase-02`

## What you learn

How to decide what an LLM must **not** be allowed to do, and how to make that
boundary structural rather than a plea in a prompt. Also: FAO-56, which is a nice
example of a domain where the right answer is *computable*, so guessing it is
indefensible.

## The central idea

Ask a language model to run a 720-hour water balance and it will produce a number
that looks right. It will be wrong, unreproducible, and impossible to audit. The
number is also trivially computable:

```
ETc = ET₀ × Kc                         crop water use per hour
depletion = Σ ETc − Σ (rain × 0.80)    running, clamped to [0, TAW]
irrigate when depletion ≥ RAW          RAW = TAW × MAD fraction
```

Nine lines of Python. So the model never sees this as a task; it sees the
*result*, and its job is to judge and explain it.

The same applies to spray timing. Whether an hour is sprayable is three gates:
Delta-T inside a band, wind inside a band, and no rain within the rain-fastness
window. All three are thresholds against numbers we have. So the deterministic
layer emits *candidate windows*, and the model may only narrow them.

## Decisions

- **Physics in Python, judgement in the model.** `features.py` computes; it never
  phrases. `agents.py` phrases; it never computes.
- **Crop parameters are injected, not global.** `Deps` is a frozen dataclass with
  `slots`, passed as `deps_type`. Frozen because immutable deps are safe to share
  across concurrent async runs; injected because onboarding table grapes must be
  `Deps(kc=0.85, ...)` and not a code change. `grep "0.7"` finds Kc exactly once.
- **Depletion is clamped to `[0, TAW]`.** The dataset has no irrigation log, so
  the raw sum is an *atmospheric demand* signal, not a measured soil state. The
  clamp keeps it physical, and a note flags that it is an upper bound.
- **Missing ET₀ hours are skipped, not zero-filled** — so depletion is a lower
  bound, and the error is in the safe direction.
- **A missing spray-critical hour is excluded from windows, never interpolated.**
  You cannot invent a wind speed and then advise someone to spray during it.
- **The mechanical trigger is emitted; the deficit-irrigation call is not.**
  Whether to partially refill or hold off is genuine agronomic judgement, and it
  is left to the agent on purpose. The deterministic layer says only "depletion
  has crossed RAW".

## Read this

- `src/vinea/features.py` — the water balance, `_classify_hours`,
  `detect_spray_windows`
- `src/vinea/deps.py` — every tunable, with its source
- `src/vinea/contracts.py` — the typed outputs the agents will have to satisfy

## The trap

Gate order is reporting order. `_gate_reason` returns the **first** failing check
in a fixed sequence — Delta-T band, then wind class, then rain-fastness, then the
optional index. On the committed data, 11:00–19:00 tomorrow violates *both*
Delta-T (>10 °C) and wind (>4.2 m/s), and every one of those hours is reported as
a Delta-T failure.

That is fine for a human summary and actively misleading if you treat the reason
tally as a diagnosis of *which* constraint dominates the site. If you want that,
count violations per gate, not reported reasons.

## Try it

```bash
uv run vinea --features-only
uv run pytest tests/test_features.py -v
```

Verify the physics by hand. Sum the ET₀ column of the history CSV: 197.11 mm.
Times Kc 0.70 = 137.98 mm, which is what `cumulative_etc_mm` reports. Rain is
5.6 mm; at the 0.80 effective fraction that credits 4.48 mm, so depletion is
133.5 mm. Nothing is hidden.

Then change one number in `deps.py` — set `kc=0.85` — and watch every downstream
figure move coherently, with no other edit anywhere. That is what "the crop is a
parameter" buys.
