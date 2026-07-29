# ADR-009: Tenant isolation in the database, not in the queries

- **Status:** accepted
- **Date:** 2026-07-29
- **Milestone:** phase 17 (security hardening)

## Context

Tenant isolation is 29 `WHERE tenant = :tenant` clauses across `repository.py`,
`queue.py`, `scheduler.py`, `worker.py` and `tenancy.py`. The API layer above them
is careful — `scoped_tenant` is one dependency rather than a per-route check that
can be forgotten — but below it, isolation is a convention held by every author of
every future query. One omission serves another grower's advisories with a
correct-looking 200, no error and no log line.

This project has made the same trade three times and named it each time: the
unique index behind advisory idempotency, the partial index behind
one-open-config-per-block, and phase 14's spend ceiling on a LiteLLM virtual key.
*A rule in code is a promise; in the database it is a guarantee.*

## Decision

**Postgres row-level security on every table with a `tenant` column, enforced
through a non-superuser role assumed on connection checkout, scoped per
transaction with `set_config`.**

Four parts, and each was necessary — three of them discovered by the previous one
failing:

1. `ENABLE` **and** `FORCE ROW LEVEL SECURITY` — a table's owner bypasses its own
   policies without `FORCE`.
2. A **`vinea_app`** role: `NOSUPERUSER`, `NOBYPASSRLS`, `NOLOGIN`, assumed with
   `SET ROLE` in a `connect` listener.
3. The `SET ROLE` is **committed** in that listener.
4. The tenant scope is recorded on the session and **re-applied on `after_begin`**.

## Rationale

### Why not simply keep the WHERE clauses

They work, until one is missing. The failure is silent, indistinguishable from
correct behaviour in a response, and discovered by the wrong person.

### Why FORCE is not enough, and how that was found

The first version of the migration enabled and FORCEd RLS on all five tables,
applied cleanly, and reported `relrowsecurity = true` everywhere. It was
**completely inert**. `vinea` is the container's bootstrap role —
`rolsuper = true, rolbypassrls = true` — and superusers bypass row security
unconditionally; FORCE does not reach them. A scoped session still read every
tenant's rows.

That is the worst kind of security control: one that reports success while doing
nothing, because it stops anyone looking. It is why `test_security.py` asserts
row *counts* rather than configuration flags, and why the first test in the file
checks `rolsuper is False`.

### Why `SET ROLE` on checkout rather than inside the scoping call

With the role applied only inside `scope_to_tenant`, a session that forgot to
scope itself ran as the superuser and saw everything. That replaces "29 places to
forget a WHERE" with "one place to forget a call" — an improvement, not a
guarantee.

Applied at connection checkout, a session that declares nothing **sees nothing**:
it is already the restricted role, `current_setting('vinea.tenant', true)` is
NULL, `tenant = NULL` is NULL, every row is filtered. Forgetting became the safe
direction.

Alembic is unaffected: `migrations/env.py` builds its own engine and never calls
`make_engine`, so DDL still runs as the owner. That separation is load-bearing —
a migration running as `vinea_app` could not create a table.

### Why the `SET ROLE` must be committed

`SET ROLE` is transactional in Postgres. Issued inside the implicit transaction
the listener's cursor opens, it is undone by the ROLLBACK SQLAlchemy performs when
the connection returns to the pool. The first `engine.connect()` reported
`vinea_app`; every session after it reported `vinea`. Half the suite would have
run restricted and half not, depending on pool reuse — an intermittent security
control, which is worse than none.

The same fact bit again in the opposite direction: the test fixture's `RESET ROLE`
before `TRUNCATE` was committed, so the pooled connection stayed the owner for the
rest of its life and the whole suite went green about a control it was no longer
exercising. Both are fixed; both are in the phase doc, because the lesson is
"transaction-scoped settings and long-lived connections interact badly in *both*
directions".

### Why the scope is re-applied on `after_begin`

`SET LOCAL` is discarded at COMMIT — exactly the property that makes it safe on a
pooled connection, and a trap for any session that commits and keeps working. The
worker does precisely that: `process_one` commits the advisory and its task, then
`run_worker` loops and claims again. Without re-application the second claim ran
unscoped, saw nothing, and surfaced far from the cause as *"could not refresh
instance"*.

So the scope is an *intent* stored on the session and re-issued at the start of
every transaction, which is what every caller already assumed `scope_to_tenant`
meant.

### Why `set_config`, not string-formatted `SET LOCAL`

`SET LOCAL x = $1` is not valid SQL — the value must be a literal — so the naive
version interpolates the tenant name into statement text. That would be the one
place in this codebase where a tenant name becomes executable SQL.
`set_config(name, value, true)` takes bind parameters.

## Rejected alternatives

**A database per tenant.** The strongest isolation available and operationally
absurd for a system whose entire design is one Postgres (ADR-003). Revisit only
if a tenant's own compliance regime demands it, at which point it is their bill.

**A connection pool per tenant.** Multiplies the pool by the tenant count and
makes a new tenant an infrastructure change. `SET LOCAL` on a shared pool is safe
*because* it is transaction-scoped, which is the same property that made it a trap
above — worth noticing that the two are one fact seen from two sides.

**Keeping isolation in application code and adding a lint rule.** A rule that
every query mentions `tenant` is defeated by the first legitimate cross-tenant
query, which is `claim_one`.

**A second role with no ops escape.** The genuinely stronger design: the worker
and `/ops/*` connect as a role with `BYPASSRLS`, the application connects as one
without, and nothing in the request path can opt out. Rejected *for now* on cost —
it means a second DATABASE_URL in every deployment, compose file, CI job, e2e
script and test fixture. Recorded as the revisit trigger rather than pretended
away.

**A phrase blocklist for prompt injection.** Rejected outright. It cannot
enumerate the ways to say "ignore the above" in every language a model
understands, and shipping one creates false confidence in a weak control while
drawing attention from the strong one — the output validators that stop an
injected number reaching an advisory. `security.py` bounds *size* and strips
template delimiters, and its docstring says plainly that it is not a filter.

## Consequences

**We accept:**

- **Every session must now declare its scope.** 56 tests failed the moment RLS
  became real, and that number is the honest measure of how much was resting on
  convention. The API grew `tenant_session` and `ops_session` dependencies; the
  worker and the jobs CLI declare ops scope once each; tests use an `ops_session`
  fixture or `open_ops_session`.
- **The ops escape is application-settable.** See the rejected alternative above.
- **`advisory_citations` and `eval_runs` are not policed directly** — no `tenant`
  column, reachable only through `advisories.id` with `ON DELETE CASCADE`. A leak
  there exposes a locator or a score, not advice.
- **A privilege model to maintain.** `ALTER DEFAULT PRIVILEGES` covers tables
  added by later migrations; without it, phase 18's first table would arrive
  ungranted and fail in a way that reads nothing like "you forgot a grant".

**We get:**

- A forgotten `WHERE` returns nothing instead of everything.
- Cross-tenant writes refused by the database (`WITH CHECK`), not by review.
- An enumeration test that fails when a future phase adds a tenant-scoped table
  without a policy — the gap closes itself.

## Revisit if

- Application code needs to be prevented from opting out — implement the second
  role and drop the `vinea.ops` escape.
- A tenant requires physical separation — a database per tenant becomes their
  decision and their cost.
- `eval_runs` or `advisory_citations` grow a `tenant` column, at which point they
  join the policy list and the enumeration test will say so first.
