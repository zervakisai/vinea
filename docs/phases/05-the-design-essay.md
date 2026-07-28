# Phase 5 — The design essay

`git checkout phase-05`

## What you learn

That "how would you scale this?" is a design question with a *shape*, and that
writing the answer down before building forces you to notice which parts of your
architecture were load-bearing and which were habit.

## The central idea

This phase adds no code. It adds [`DESIGN.md`](../../DESIGN.md) — three topics,
researched and argued, with concrete tools, trade-offs and a recommendation each:

| | Topic | Later built in |
|---|---|---|
| **B1** | Scaling — batch fan-out, model routing, retries, autoscale, tenancy | phases 6, 8 |
| **B2** | Monitoring, debugging & improving the advice | phases 9, 12 |
| **B3** | Managing prompts without redeploying | phase 12 |

Phases 6–12 are that document being carried out. Reading them in that order is the
point: you can check the argument against what it actually cost to build.

## Why the boundary keeps paying

Every one of the three topics turns out easier because of the phase-2 decision:

- **Scaling** — the physics is Python, so most load never reaches a model at all.
  A router reading the deterministic features can send clear-cut days to a small
  model or skip the decision call entirely. Cost collapses to the genuinely
  borderline vineyards.
- **Evaluation** — the physics is exact, so there is **ground truth**. For
  irrigation and spray *decisions* you can score the model precisely instead of
  asking another model's opinion.
- **Prompts** — the numbers live in `deps`, so the registry only ever holds
  *wording*. A registry outage degrades phrasing, never the decision.

None of that was planned in phase 2. It falls out of having drawn the line in the
right place, which is the strongest argument for drawing it early.

## Read this

- [`DESIGN.md`](../../DESIGN.md) — the whole essay
- `git log --oneline phase-04..phase-05` — one commit, no `src/` changes

## The trap

A design essay written *after* the code is a description; written *before*, it is a
prediction — and predictions can be wrong in ways worth keeping visible. Some of
this one was:

- It proposed provider **Batch APIs** for the overnight window. Phase 8 builds the
  queue and the workers, but not batch submission. The claim is untested here.
- It assumed a **region-shared forecast** would collapse the spray FeatureBuilder
  pass for every grower in a region. Phase 8's cache is per-tenant and exact, not
  region-scoped.
- Its `# TODO(B2)` markers in phase 1–4 code were written before phase 9 existed.
  They point at a design that mostly survived, which is only interesting because
  it *could* have failed.

Resist the urge to quietly edit the essay to match what you built. The gap between
proposal and implementation is the most instructive thing in the repository.

## Try it

```bash
git diff phase-04 phase-05 --stat        # docs only
```

Then read B2's section on the circularity trap — the same oracle used inline as a
retry guard *and* as the eval metric — and go look at how phase 12 keeps those two
code paths separate via `advisories.pre_correction_output`. That is a subtle bug
caught on paper, before it was ever written.
