# Phase 16 — Context engineering

`git checkout phase-16`

> The problem statement and decision table below were written **before** the
> build. Everything from *What the measurements said* onward was written after —
> and the phase's central promise turned out not to be available in the form the
> essay assumed.

## What you learn

That "context engineering" is measurement before it is editing — and that when
you reach for the instrument that will prove your change was safe, you should
check what it actually measures first.

> **This paragraph originally read** *"…the only honest proof a prompt got
> smaller without getting worse is the eval gate you built two phases ago."*
> That turned out to be false about this system's eval gate, which scores stored
> outputs and never assembles a prompt. Finding that out was the phase; the
> original wording is preserved in **The trap** rather than quietly corrected,
> because the mistake is the lesson.

## The problem, measured

Every previous phase in this project reached for a number before making a claim.
This one starts there, because the finding is uncomfortable.

Here is the entire context each agent receives on the committed dataset, run
date 2026-07-28, with the corpus ingested:

| component | chars | ~tokens |
|---|---:|---:|
| irrigation: static instructions | 762 | 190 |
| irrigation: context block | 272 | 68 |
| irrigation: user input | 528 | 132 |
| **irrigation: retrieved passages (phase 15)** | **4 217** | **1 054** |
| spray: static instructions | 747 | 186 |
| spray: context block | 347 | 86 |
| spray: user input | 2 045 | 511 |
| **spray: retrieved passages (phase 15)** | **4 221** | **1 055** |
| irrigation leg, total | 5 779 | 1 444 |
| spray leg, total | 7 360 | 1 840 |

**Retrieval is 64% of the context of both legs.** The irrigation leg was 1 562
characters before phase 15 and is 5 779 after — 3.7× — and no measurement was
taken at the time. The constant responsible is one line:

```python
TOP_K = 3   # rag/retrieve.py
```

chosen because three is a reasonable-looking number. Phase 15's own comment
admits it, and hands the bill here:

> Three, and phase 16 will have to justify any increase in tokens rather than
> inheriting it: this is exactly the kind of constant that grows to ten because
> nobody measured what it cost.

Three further findings from the same table:

**1. The spray leg's *input* is bigger than its instructions.** 2 045 characters,
of which about 1 400 is a 24-row per-hour table. That table is not obviously
wrong — the candidate windows above it are computed deterministically, and the
per-hour rows are what let the model explain *why* 20:00 was excluded. But every
one of those 24 rows carries `index=None`, because this dataset has no vendor
spray index. Roughly 260 characters spent restating an absence 24 times, when the
absence is one fact.

**2. Nothing bounds any of this.** Raise `TOP_K`, ingest a second corpus, or let
a grower's config grow, and the context grows with it. The only signal is a bill,
and phase 14 made that bill visible — but only *after* the night.

**3. And the counting itself has a hole.** Phase 14's token columns are populated
by `MeteredModel`, which only wraps the gateway path:

```python
if not settings.enabled:
    return model      # a plain string. Nothing is metered.
```

So on a laptop, or in any deployment without a gateway, `input_tokens` is NULL —
the system that just tripled its own context cannot see it. That is a gap phase
14 created deliberately (cost is the gateway's to report) and phase 16 has to
answer, because *tokens* are not cost and were never the gateway's to own.

## The shape of the work

Three things, in this order, and the order is the lesson:

**Measure.** A `context` module that reports the size of every component of every
leg, offline, with no gateway and no model. If the accounting only works in
production, it will be consulted after an invoice rather than before a change.

**Bound.** A token budget per leg, enforced by dropping the lowest-ranked passage
first. Not truncating one — see below.

**Prove.** Slim the instructions, then show the eval gate did not move. Phase 12
built an asymmetric scorer and a golden replay for exactly this moment: a change
that makes the prompt smaller and the advice worse is a change that looks like a
win in every metric except the one that matters.

## Decision table

| Question | Options | Verdict |
|---|---|---|
| **How are tokens counted offline?** | `chars / 4` estimate · a real tokenizer (`tiktoken`) · the provider's `count_tokens` endpoint | **estimate, calibration wired** — a tokenizer is per-vendor and a second one in the image; the endpoint is a network call per measurement and needs a credential. **Correction after building:** calibrating against `input_tokens` alone is impossible — it takes *paired* numbers from one request, so phase 16 adds `advisories.context_chars` beside it. As shipped the pair is empty, and the estimator is an unverified assumption that says so |
| **What gets trimmed when the budget is exceeded?** | truncate the longest passage · drop the lowest-ranked passage whole · summarise passages with a model | **drop whole, lowest-ranked first** — truncating a passage mid-sentence breaks phase 15's central promise: the citation still says "Chapter 8" and the quote no longer says what Chapter 8 says. A half-quoted source is a *wrong* source. Summarising puts a model between the corpus and the citation, which makes the citation a claim about a summary |
| **Is `TOP_K = 3` right?** | keep · lower to 2 · make it a token budget rather than a count | **keep, and add a budget** — measured: recall saturates at 3 (0.83 / 0.92 / **1.00** / 1.00 for k = 1..4). Dropping to 2 costs eight points of recall to save 289 tokens; a fourth passage costs 300 tokens for nothing. The count stays and a *token* ceiling sits between three and four, because passages are not equal length |
| **Which instructions get slimmed?** | all three agents · only the demonstrably redundant · none, measure first | **only the demonstrably redundant** — and that turned out to be 211 characters in one leg. The eval gate could not have adjudicated more than that anyway; see **The trap** |
| **Does the per-hour table stay?** | keep · drop entirely · drop all-None columns | **drop all-None columns** — the rows carry the explanation; `index=None` repeated 24 times carries one fact. "Missing stays missing" was always about not *fabricating* a value, never about restating an absence per row |

## What must not happen

**No context change ships on a vibe.** The gate is phase 12's, unchanged: the
asymmetric scorer (recall on *should irrigate* is the number to keep near 100%)
and the golden replay. A slimming that saves 30% of tokens and drops recall by
two points is a regression that will be discovered by a grower.

**The retrieved passages stay whole.** Phase 15's promise is that a citation
points at something a reader can check. Every trimming strategy here preserves
that or is rejected.

**The deterministic core stays out of it.** Instruction text is prose; features
are numbers. Nothing in this phase touches how a number is computed — and if
slimming an instruction appears to change an advisory's *number*, that is not a
context finding, it is a guardrail finding, and it belongs in phase 12's
machinery rather than in a smaller prompt.

## What the measurements said

**`TOP_K = 3` is right, and now for a reason.** Phase 15's recall gate, re-run at
each depth against the same 12 labelled questions:

| top_k | recall | passage tokens |
|---:|---:|---:|
| 1 | 0.83 | 286 |
| 2 | 0.92 | 576 |
| **3** | **1.00** | **865** |
| 4 | 1.00 | 1 165 |

Dropping to two saves 289 tokens and costs **eight points of recall**. Paying for
a fourth costs 300 tokens and buys nothing measurable. Three is where it
saturates. That answers the essay's open question 2 with a number instead of a
preference, and it sets the budget: `DEFAULT_LEG_TOKEN_BUDGET = 900` sits above
three passages and below four.

Which makes the budget **forward-looking rather than corrective**. It trims
nothing today. It exists so a later phase raising `TOP_K`, or a corpus whose
chunks run longer, cannot grow the prompt without someone deliberately raising
the number and having to justify it against that table.

**The spray input lost 211 characters and gained a fact.** `index=None` on 24
per-hour rows became one line — *"Not reported by this feed for any hour: spray
index."* — and the leg went 2 045 → 1 834 chars. Only columns that are None for
*every* hour are dropped; a field that is None for some hours still varies, and
hiding those gaps would be exactly the fabrication "missing stays missing"
forbids.

**After both changes:**

| leg | before | after | retrieval share |
|---|---:|---:|---:|
| irrigation | 5 779 | 5 779 | 73% |
| spray | 7 360 | 7 149 | 59% |

The honest reading: instruction slimming moved 3% of one leg. **Retrieval is
still the overwhelming majority of the context, and the lever that matters is the
one phase 15 installed, not the prose anybody writes.** That is worth saying
plainly, because "context engineering" invites an afternoon of rewording
instructions that could not possibly have mattered here.

## Open questions for the build — answered

1. **How wrong is `chars / 4` on this corpus?** *Unknown, and the machinery to
   find out is wired and empty.* Calibration needs **paired** numbers from the
   same request — characters out, tokens counted — and tokens alone calibrate
   nothing, because dividing a token total by an estimate just returns the
   assumption. So `MeteredModel` now records both, and `advisories.context_chars`
   sits beside `input_tokens`: NULL together, populated together. As this
   repository ships, `python -m vinea.context --calibrate` prints:

   > No advisory carries both context_chars and input_tokens, so there is nothing
   > to calibrate against.

   That is the honest state. No provider key travels with this repo, so nothing
   has ever been metered here, and the estimator remains a *stated assumption*
   rather than a measured one. Which is exactly why the function is named
   `estimate_tokens`.

2. **Does dropping to `TOP_K = 2` cost recall?** Yes — eight points, 1.00 → 0.92,
   to save 289 tokens. Measured, table above. Answered, and against.

3. **Where does the budget live?** A module constant, not `grower_config`.
   Phase 6's "new crop = config change" argument applies to values a *grower*
   sets; a context ceiling is an engineering choice about a prompt, and nobody
   operating a vineyard has an opinion about it. Making it per-tenant would add a
   column, a migration and a mapping to support a knob with one setting.

4. **Is the spray per-hour table earning its 1 400 characters?** Not established,
   and deliberately not removed. The candidate windows above it are computed
   deterministically, so the table's only job is *explanation* — letting the model
   say why 20:00 was excluded rather than asserting it. Whether the explanations
   get worse without it cannot be answered by anything in this repository (see
   **The trap**), so it stays, and the question is recorded rather than resolved
   by guess. Only the all-None columns went.

## Decisions

**Accounting is offline and estimated, on purpose.** A real tokenizer is
per-vendor: `VINEA_MODEL` names any of five providers, so one tokenizer is wrong
for four of them, and five is five dependencies in an image already at 391 MB.
Worse, it would be *precisely* wrong — four significant figures belonging to a
model this deployment is not using. The provider's `count_tokens` endpoint is
exact and needs a credential and a network call per measurement, which puts the
accounting out of reach of a laptop and CI, the two places it is meant to be
consulted *before* a change ships.

**`context_chars` is a new column, and it is the other half of a pair.** Phase
14's `input_tokens` alone cannot calibrate anything. Both are written by
`MeteredModel` at the one point that sees the fully-assembled request, so they
are NULL together and populated together. Not recomputable — the passages that
made up that night's prompt depend on a corpus that may since have been
re-chunked, the same argument as `advisory_citations.locator`.

**The budget bounds retrieved passage text only.** Not the instructions, the
context block or the input: those are the system's own reasoning and are not
negotiable against a corpus. Not the framing either, which is a fixed ~125 tokens
whatever survives.

**Whole passages, never truncated, never summarised.** A truncated passage still
arrives labelled "Chapter 8" and the label becomes a claim about text the model
never saw the end of. Summarising is worse by a subtler route: the citation would
point at a source the *summary* was drawn from, and the model would be quoting
itself with FAO's name attached — the same failure ADR-007 rejected semantic
caching for.

## The trap

**The proof this phase promised does not exist, and finding that out was the
phase.**

The essay says, in its opening line: *"the only honest proof a prompt got smaller
without getting worse is the eval gate you built two phases ago."* Then I went to
run it, and read how it is actually invoked:

```python
outcome = run_golden_eval(
    s,
    task=lambda _i: _advisory_with(depletion=133.5, recommend=True),   # a stub
    ...
)
```

`run_golden_eval` takes a `task` callable. Every existing caller passes a
**stub advisory**. The gate scores *outputs* — it never assembles a prompt, never
calls a model, and is completely blind to instruction text. It would have passed
identically if I had deleted the instructions entirely.

So the sentence in the essay was wrong, and "the tests still pass" would have
been a true statement about a claim I had not made. What can be proved offline is
narrower and worth naming precisely:

> **A prompt contract.** Every fact the output validators and the deterministic
> oracles depend on is still present after slimming. Nothing load-bearing was
> deleted.

`tests/test_context.py` asserts exactly that and says at the top what it does not
assert: that the model's prose is no worse. Nothing offline can. That needs a live
model and, for the part that matters — *is this rationale useful to an
agronomist?* — a human, which is what phase 12's two annotation queues are for.

**The second trap is smaller and more embarrassing.** The budget's first
constant was 900 with a comment claiming it sat "deliberately BELOW today's usage
so the mechanism is exercised". It did not: three passages cost 865 tokens, so
900 admitted all three and trimmed nothing. The number was right and the reason
was invented. Measuring recall at each depth produced the real reason — 900 sits
above three and below four, where recall saturates — and the comment now carries
the table instead of a claim about what it does.

Both traps are the same shape as phase 15's, one layer up: **check what the
instrument actually measures before quoting it.**

## What this phase did not achieve

Instruction slimming moved **3% of one leg**. Retrieval is still 73% of the
irrigation prompt and 59% of the spray prompt. The lever that matters is the one
phase 15 installed, and it is already at its measured optimum.

Which is the useful negative result: *"context engineering"* invites an afternoon
of rewording instructions, and on this system that afternoon could not have
mattered. The measurement is what says so — and taking the measurement first is
the entire method.

## Try it

```bash
# 1. What is in the prompt, right now, offline.
python -m vinea.context
#   retrieved passages      4217      1054
#   TOTAL irrigation        5779      1444
#   retrieved passages: 73% of this leg

# 2. The calibration that is wired and empty — and says so.
python -m vinea.context --calibrate

# 3. The budget refuses a fourth passage, and never cuts one in half.
uv run pytest tests/test_context.py -v -k "budget or rank"

# 4. The prompt contract: what slimming was not allowed to remove.
uv run pytest tests/test_context.py -v -k "load_bearing or none"

# 5. See for yourself that the eval gate does not touch prompts.
grep -n "task=" tests/test_evals.py
#   task=lambda _i: _advisory_with(...)   <- a stub advisory, not the graph
```

## The invariant

```bash
git diff --ignore-blank-lines phase-15 phase-16 -- \
  src/vinea/features.py src/vinea/contracts.py src/vinea/deps.py \
  src/vinea/graph.py src/vinea/reconcile.py src/vinea/pipeline.py     # must be empty
```

This phase edits prose and counts tokens. If shortening an instruction requires
changing what `features.py` computes, the instruction was carrying a computation
— and finding that would be the most interesting possible outcome of this phase,
to be written down here rather than fixed by editing the core.
