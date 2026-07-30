# Contributing

## Comments

This codebase was originally written as a teaching sequence, and its comments
carried that: references to build phases, section codes from a design document,
and narration about what the author had just learned. All of it made sense to
someone reading the phases in order and none of it makes sense to someone reading
`worker.py` because a task failed at 03:00.

The rule now: **a comment explains why the code is the way it is, to someone who
has never read anything else in this repository.**

### Write

- **Why, not what.** `# SET LOCAL, not SET: a plain SET rides the pooled
  connection into the next request.` The reader can see it is `SET LOCAL`.
- **The failure you are preventing.** `# COMMIT is load-bearing: SET ROLE is
  transactional, and the pool's rollback-on-return undoes it.`
- **Constants that encode a measurement.** A number with no reason is a number
  someone will change. Put the measurement next to it.
- **ADR references.** `(ADR-003)` is fine and useful — the ADRs are part of the
  product, they are in this repository, and they carry the argument so the comment
  does not have to.

### Do not write

| Don't | Because |
|---|---|
| `# phase 14 added this` | The reader is not reading phases. `git log` and `docs/engineering-log/` have it. |
| `# S4.3`, `# B2-1` | Codes from a design document nobody opening this file has. |
| `# see DESIGN.md` | Same. Point at an ADR, or state the reason inline. |
| `# the lesson here is…` | This is not a lesson; it is a vineyard irrigation service. |
| `# we decided to…`, `# our approach` | Say what the code does and why. Nobody needs the committee. |
| A paragraph where a sentence works | Comment volume is a maintenance cost like any other. |

`tests/test_style.py` enforces the first three mechanically. It runs offline and
is deliberately blunt: if it flags something you believe is right, the fix is
usually to state the reason rather than the reference.

### Docstrings

Module docstrings say what the module is for and what rule governs it. Function
docstrings say what a caller needs to know that the signature does not — the
failure modes, what is not guaranteed, what the caller owns. Skip them where the
name and the types already say it.

The history is not lost. `docs/engineering-log/` keeps the phase-by-phase
narrative, and every decision is in `docs/adr/`. The code is for operating the
system.

## Working on it

```bash
uv sync                       # everything, including dev
docker compose up -d postgres # pgvector/pgvector:pg16
uv run alembic upgrade head
uv run pytest                 # offline by default; DB tests skip without Postgres
uv run ruff check .
```

Before opening a PR:

- `uv run pytest` green with a database reachable (`VINEA_TEST_DATABASE_URL`).
- `uv run ruff check .` clean.
- `uv run alembic check` reports no drift between models and migrations.
- If you touched the schema, `./infra/kind-e2e.sh --cleanup` — the migration hook
  is the thing that gates a real deploy.

## Schema changes

Expand-only, forward-only. Add nullable columns; do not rename or narrow in one
release. The migration runs as a pre-upgrade Helm hook, so a migration that fails
stops the deploy before any new pod serves traffic — which is the point.

`corpus_chunks.embedding` is reserved and never written (ADR-011). Do not start
writing it without redoing the retrieval benchmark that removed it.

## What not to change without an ADR

- The six files that compute the agronomy: `features.py`, `contracts.py`,
  `deps.py`, `graph.py`, `reconcile.py`, `pipeline.py`. Every number a grower sees
  is produced there, checked there, and never produced by a model. This is also
  the security boundary: an injected instruction cannot change a number it cannot
  reach.
- Anything ADR-003 rejected: a second stateful system needs an ADR that defeats
  the standing argument, not a pull request.
