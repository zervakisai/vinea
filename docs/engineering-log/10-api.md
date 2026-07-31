# Phase 10 — API

`git checkout phase-10`

## What you learn

How to keep a web layer thin enough that deleting it would break nothing — and why
"the endpoint returns before the model runs" is an architectural property rather
than a performance trick.

## The central idea

**The rule:** the API does not run agents and does not compute anything. Every
write is `queue.enqueue`; every read is a repository call.

The consequence worth internalising:

```
POST /advisories  →  202 Accepted + task id   in milliseconds
```

It returns *before any model runs*, because running the model is the worker's job
(phase 8), reached through the queue. **The API's latency is a database write, not
an LLM call.**

This is the same relationship the Open-Meteo adapter has to `WeatherRow`: a thin
skin over a seam that was already stable before the skin arrived. If you deleted
`src/vinea/api/`, the worker, the graph, the guardrails and the contracts would all
still stand.

## Decisions

- **202, not 200.** The status code tells the truth about what happened: work was
  accepted, not completed. A 200 with a synthesised body would be a lie that
  clients then build timeouts around.
- **Per-tenant API keys, parsed from one env var.** `VINEA_API_KEYS` as
  `key1:tenantA,key2:tenantB`, plus a separate `VINEA_OPS_KEY` for operator
  endpoints. Crude, honest, and adequate — OIDC is the production answer and the
  seam for it is `auth.py`.
- **Tenant scoping is enforced in the repository, not the route.** A route that
  forgets a `WHERE tenant = ...` is a data leak; putting the scope one layer down
  means the route cannot forget.
- **`schemas.py` is separate from `contracts.py`.** The wire format is allowed to
  evolve independently of the domain model. Serving `contracts.py` directly couples
  your public API to an internal refactor.

## Read this

- `src/vinea/api/main.py` — the routes, all thin
- `src/vinea/api/auth.py` — key → tenant, and the ops key
- `src/vinea/api/schemas.py` — the wire format
- `tests/test_api.py` — including cross-tenant access attempts

## The trap

`VINEA_API_KEYS=key-acme:acme,key-olivares:olivares` is a **comma-separated list of
bearer tokens in an environment variable**. It has no rotation, no expiry, no
revocation, no hashing, and no audit trail. If it leaks, every tenant leaks at once
and you find out never.

It is fine for a demo and for a repository that is teaching something else. It is
not fine in production, and the danger is precisely that it *works* — nothing will
ever prompt you to replace it. `auth.py` is the seam; treat swapping it for OIDC or
a real key store as a prerequisite for a first customer, not a nice-to-have.

> **Closed 2026-07-31 by [ADR-012](../adr/012-api-keys-in-the-database.md).** Keys
> are rows in `api_keys`: hashed, revocable with one `UPDATE`, with a label and a
> `last_used_at`, and every call logged in `access_log`. The paragraph above turned
> out to be right about the mechanism *and* about the danger — it worked for eight
> more phases, and nothing prompted a replacement until somebody went looking for
> what was still missing.
>
> The commands below are from before that, and `key-acme` no longer authenticates.
> Mint one with `python -m vinea.keys issue --tenant acme --label "…"`.

## Try it

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn vinea.api.main:app --port 8099 &

curl -s -XPOST localhost:8099/advisories \
  -H 'x-api-key: key-acme' \
  -d '{"run_date":"2026-07-28"}' | jq
# -> 202, a task id, and it came back before any model ran

uv run python -m vinea.jobs work --max-tasks 1
curl -s localhost:8099/advisories/2026-07-28 -H 'x-api-key: key-acme' | jq
```

Then try reading `acme`'s advisory with `key-olivares` and watch the repository
scope refuse it. Then check `tests/test_api.py` for the test that asserts exactly
that — cross-tenant isolation is the one thing here you should never take on faith.
