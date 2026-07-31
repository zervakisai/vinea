# ADR-012: API keys in the database, hashed and revocable — and a log of who used them

- **Status:** accepted
- **Date:** 2026-07-31
- **Supersedes:** the environment-variable key mapping introduced with the API

## Context

Authentication was one environment variable:

```
VINEA_API_KEYS="key-acme:acme,key-olivares:olivares"
VINEA_OPS_KEY="ops-secret"
```

Parsed on every request, no caching, fail-closed when unset. As a first
implementation that was the right size, and the phase that built it said so
plainly: *"a simple header check for now, with OIDC/JWT as a clearly marked seam."*

Three things were wrong with it, and none of them are fixable without a table.

**A key could not be revoked in less than a deploy.** Removing one means editing a
Secret and restarting every pod that reads it — `envFrom` is resolved at pod
creation. Between *"this key is compromised"* and *"this key stops working"* sat a
rolling restart, during which the compromised key kept working. The one moment
revocation matters is the one moment it was slowest.

**Every key was readable in plaintext.** `kubectl describe pod`, `/proc/<pid>/environ`,
a shell's history, a `.env` in a dotfiles backup, a CI log that echoed the
environment — all of them yielded every tenant's credential. The blast radius of
*read access to a pod spec* was *authenticate as any tenant*.

**There was no history.** No record of when a key was issued, for what, by whom, or
whether it had been used since March. "Which of these can we retire" was a
conversation rather than a query, and the safe answer to that conversation is
always "leave them all".

And nothing recorded who called what. A tenant reporting *"someone else is using my
key"* could be answered only with a shrug.

## Decision

**API keys are rows in `api_keys`, stored as SHA-256, revocable with an `UPDATE`,
and minted by `python -m vinea.keys`. Every authenticated call and every rejection
is recorded in `access_log`, which is under row-level security.**

`VINEA_API_KEYS` and `VINEA_OPS_KEY` are no longer read by the application.
`python -m vinea.keys import-env` stores the hashes of existing keys so the ones in
circulation keep working across the cutover.

## Rationale

### Why SHA-256 and not bcrypt or argon2

This looks like the classic mistake and is its mirror image, so the reasoning is
written down rather than left to be reverse-engineered.

A slow KDF exists because **passwords are low-entropy and human-chosen**. A leaked
password hash is attacked by trying likely inputs, and a cost factor is what makes
each attempt expensive. An API key here is 32 bytes from `secrets.token_urlsafe`.
There is no dictionary of likely values, and no cost factor that makes 2²⁵⁶ more
infeasible than it already is.

What a slow KDF *would* add is ~100 ms of CPU on every authenticated request, on
the grower-facing read path, defending against an attack that does not apply. The
same reasoning inverted — a fast hash over a user-chosen secret — is a real
vulnerability, which is exactly why the distinction needs stating.

### Why one table for tenant and ops keys

They are the same object with different reach: a credential, a scope, a revocation
state. Two tables would mean two revocation paths, and the second one is the one
that gets forgotten during an incident.

A check constraint keeps the two shapes honest — a tenant key must name a tenant,
an ops key must not — so a future caller that bypasses `issue()` cannot create a
tenant key that authenticates to nothing.

### Why the access log is a second table and not two more columns

`api_request_samples` already records route, method, status and time, and the
overlap is obvious enough that merging them was the first design.

They sample **different populations, on purpose**. The latency table records two
GET routes, deliberately narrow, so that liveness probes firing every few seconds
cannot drown the few hundred requests a grower actually makes — *an SLI measured
over probe traffic reports the health of the probe*. The access log records every
authenticated call and every rejection, including the writes and `/ops/*`, because
a security record that omits the interesting routes is not a security record.

Merging them forces one of those two definitions to change, and the one that
changed would be quietly wrong: a latency percentile whose denominator silently
grew to include health checks reads as an improvement.

The duplicated columns are the price. It is smaller than the price of an SLI whose
meaning moved.

### Why the lookup runs under the ops escape

`api_keys` is read *before the tenant is known* — that is what authentication is —
so it cannot run under a tenant row policy. This is the bootstrap ordering every
credential system has, not a hole opened here.

What is avoidable is a *tenant-facing* query reaching the table, and both tables
carry the standard policy so that is impossible rather than merely unwritten. A
test asserts a tenant-scoped session sees only its own key rows.

Rows from failed authentication have `tenant = NULL`. The policy needs no special
case for them on the read side: `NULL = 'acme'` is NULL, so they are invisible to
every tenant and visible under ops — which is correct, since a rejected credential
is an operator's business. The `WITH CHECK` clause does need the ops disjunct, or
it would refuse to let ops *write* them.

### Why `outcome` and not just a status code

A `status_code` says the request failed. `outcome` says *how the credential failed*,
and the four cases want four different responses:

| outcome | what it means | what to do |
|---|---|---|
| `no_key` | a client sent nothing | fix the client |
| `unknown_key` | a key nobody issued | probing, or a stale config |
| `revoked` | a key that should already be dead | **investigate** — someone still holds it |
| `expired` | a rotation nobody finished | issue a new one |
| `wrong_tenant` | a valid key against another tenant's path | **investigate** |

One status code, five situations. Collapsing them would make the most alarming two
indistinguishable from the most boring one.

### Why minting is a CLI and not a migration or an endpoint

A migration that mints a credential writes it into a file that every deploy
replays, and everyone who can read the migration history can then authenticate. An
endpoint that mints credentials needs a credential to reach it, which is the same
bootstrap problem one level further out.

So: a command run by a person with database access — the smallest set that already
has everything anyway. The key is printed once and only its hash is stored, which
is what makes a leaked database not a leaked credential.

## Consequences

**We accept:**

- **The audit trail can silently drop rows.** The `access_log` write is wrapped and
  swallowed: an audit entry that can 500 a grower's morning read has inverted its
  own priority. So an empty log is not proof an attempt did not happen. Tested, and
  stated here rather than discovered later.
- **Two database round trips per authenticated request** — one to verify, one to
  log — on top of the request's own. Affordable at a few hundred requests a day;
  it would not be at a thousand a second, and the fix then is a cache with a TTL,
  which trades back exactly the revocation latency this ADR bought.
- **A lost key cannot be recovered**, only reissued. That is the property, not a
  gap.
- **`last_used_at` is accurate to the hour**, not the second. Writing it per request
  would put an `UPDATE` on the read path to sharpen a timestamp nobody reads at that
  resolution.
- **A breaking change for existing deployments.** `VINEA_API_KEYS` stops working.
  `import-env` is the migration path and keeps circulating keys valid, but somebody
  has to run it.

**We get:**

- Revocation in one `UPDATE`, effective on the next request. Proven in the e2e
  against pods that were already running when the key was minted, so it is a test
  of the lookup rather than of process startup.
- A database dump that contains no usable credential.
- `python -m vinea.keys list` — what exists, for what, last used when.
- Expiry, for anyone who wants rotation, and *no default expiry*, because an expiry
  nobody chose is a rotation policy invented by a default.
- An answer to "who called what, and did anything get refused".

## Rejected alternatives

**Keep the environment variable and add a revocation list.** Two sources of truth
for the same question, and the failure mode is a revoked key that still works
because one of them was not updated — the precise failure being fixed.

**OIDC / JWT now.** The right destination and the wrong step. It needs an identity
provider, which for one operator and two demo tenants is more infrastructure than
the thing it secures (ADR-003). The seam is unchanged: `authenticated_tenant` is
still one dependency returning a tenant, and a JWT implementation replaces its body
without touching a route.

**Hash with bcrypt anyway, "to be safe".** Safety theatre with a measurable cost.
See the rationale above; the honest version is to write down why the fast hash is
correct here, which is what this ADR does.

**A `revoked_keys` table instead of a column.** A join on every authentication to
answer a boolean the row already has room for.

**Log to stdout instead of a table.** Then "was this key used against another
tenant" is a question for whatever log aggregator exists — and none does, which is
the same argument ADR-010 makes against a `/metrics` endpoint with no scraper. A
row is queryable with `psql`.

## Revisit if

- **Authentication latency shows up in the read-latency SLI.** Two round trips is
  the cost; a short-TTL cache is the fix, and it trades back revocation latency
  bounded by the TTL rather than by a deploy. Measure before adding it.
- **`access_log` growth becomes a problem.** It has no retention policy today,
  deliberately — a security log that prunes itself before anyone asks a question is
  a log that answers no questions. Add one when the size is real, not when it is
  imagined.
- **More than one person needs to mint keys.** That is when the CLI becomes an
  authenticated endpoint, and when the identity behind "who issued this" needs to be
  more than a label somebody typed.
- **An identity provider arrives for any other reason.** Then OIDC costs almost
  nothing extra and this table becomes the machine-to-machine path only.
