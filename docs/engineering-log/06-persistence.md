# Phase 6 — Persistence

`git checkout phase-06`

## What you learn

How to decide what deserves a table. The useful question is not "what data do we
have?" but **"what can we not recompute?"**

## The central idea

[ADR-001](../adr/001-store-what-you-cannot-recompute.md): store what you cannot
recompute.

- **Raw observations** — irreplaceable. Yesterday's weather cannot be re-derived.
  A row.
- **LLM output with its provenance** — irreplaceable. The same prompt to the same
  model tomorrow gives different text, and you need to know what you actually told
  a grower. A row.
- **Deterministic features** — a pure function of observations plus config. A
  **cache**, with a comment saying you may drop it whenever you like.

That third line is the one people get wrong. Storing derived features as though
they were facts creates a second source of truth that can disagree with the
function that produced it, and then you have to decide which one is real.

## Decisions

- **The schema mirrors the contracts.** SQLModel, so `contracts.py` and the tables
  do not drift into two different vocabularies.
- **`mapping.py` is the only place contracts become rows.** One direction each way,
  in one file. Scatter that logic and every new field becomes an archaeology
  exercise.
- **`repository.py` is the only thing the rest of the system calls.** Nothing
  outside `db/` opens a session or writes SQL.
- **Migrations are immutable versions.** Alembic, and the tests migrate a real
  Postgres rather than calling `SQLModel.metadata.create_all` — which is what
  catches a migration that has drifted from the models. Testing against
  `create_all` tests the models against themselves.
- **`reviewer_role` is an ENUM on purpose.** An agronomist judging correctness and
  a farmer judging usefulness are different signals; flattening them into one
  anonymous `reviewer` column throws away the distinction that makes the
  human-in-the-loop queue worth having.

## Read this

- `docs/adr/001-store-what-you-cannot-recompute.md`
- `src/vinea/db/models.py` — the tables, with the reasoning in comments
- `src/vinea/db/mapping.py` — contracts ⇄ rows, one place
- `migrations/versions/` — the schema actually shipped

## The trap

The DB-backed tests **skip** when no Postgres is reachable, rather than failing:

```
SKIPPED — No VINEA_TEST_DATABASE_URL/DATABASE_URL set.
```

That is a deliberate call, and it has a real cost. A red suite that means "you
forgot Docker" trains people to ignore red, so skipping is the better default for
a laptop. But it means `uv run pytest` on a clean clone reports **115 passed, 65
skipped** — and those 65 are a third of the suite. Someone who never starts
Postgres has never run the persistence layer at all, while seeing a green suite.

CI starts the service and gets no skips, which is what makes the arrangement
honest. Locally, know which number you are looking at.

## Try it

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run pytest -m db -v          # the 65 now run
```

Then check the claim that the core did not move:

```bash
git diff --ignore-blank-lines phase-04 phase-06 -- \
  src/vinea/features.py src/vinea/agents.py src/vinea/graph.py    # empty
```

Persistence was added *around* the deterministic core, not through it.

The `--ignore-blank-lines` is not a cheat, but it is worth knowing why it is there:
this phase adopts ruff, which removed exactly one blank line from `features.py`.
Without the flag the diff is one line long, and a reader who ran the command
without it would rightly wonder what else had been quietly changed. Nothing was.
