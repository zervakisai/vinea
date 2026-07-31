# 19 — Closing the gaps (v0.3.0)

**Tags:** `v0.2.0` → `v0.3.0`

Not a phase — a release. Phases 1–18 each added a layer; this entry removes the
distance between "demonstrates the idea" and "does the thing", and the honest way
to describe it is as a list of ways the system was quietly not doing the thing.

## What you learn

A system can pass every test it has and still carry structural gaps, because a gap
is precisely the thing no test was written for. Each item below was found by asking
*"what claim does the README make that nothing enforces?"* — the same question the
recall gate, the AST invariant and the RLS tests came from, asked one more time at
the level of the whole product.

## The six gaps

**One weather for everybody.** `weather_observations` was keyed by
`(tenant, location)` since phase 6 and every tenant got the same bundled CSV —
multi-tenancy in the schema, single-tenancy in the data path. `grower_config` gained
coordinates, the worker fetches per block and reads back from the database, and the
e2e proves two sites get different depletions. Found on the way: the shipped image's
`DEFAULT_DATA_DIR` resolved inside the venv — wrong in every image ever built, and
invisible because the only readers had never run in a cluster.

**Breaches nobody heard about.** `slo check` wrote a row and exited 1 — legible to
cron, invisible at 06:05. One webhook (`VINEA_ALERT_WEBHOOK_URL`), with the three
properties tested against a real socket: a dead webhook cannot fail the check,
unmeasured never notifies, the URL never reaches a log.

**A retriever running on defaults.** The task was "index the locator, re-measure".
The measurement rejected the locator — perfect on the easy questions, worse on the
grower-phrased ones — and found the real lever: unnormalised `ts_rank_cd` scored a
focused passage and a padded one *identically*, so insertion order was deciding
retrieval. Length normalisation took MRR 0.553 → 0.674, and the gate now holds an
MRR floor because recall@3 slept through exactly this.

**Credentials in an environment variable.** Phase 10 called it: *"no rotation, no
expiry, no revocation, no hashing, no audit trail... the danger is precisely that
it works."* Eight phases later nothing had prompted a replacement. ADR-012: keys as
hashed rows, revocation as one `UPDATE` proven against running pods, every call and
every rejection in `access_log` with *why*.

**A feedback table with no door.** `annotations` existed since phase 7 and nothing
ever wrote to it. The oracle proves the numbers; only an agronomist can judge the
advice. The POST route and the UI form under the advisory card are the door;
`promoted_to_golden` stays a curation step.

**Tracing declared permanent-debt.** ADR-010 recorded "Langfuse is not deployed"
as permanent. The retraction is the honest part: "permanent" was a judgement about
cost, not a fact about the system — and nine phases of code asserting the trace
tree shows the deterministic boundary had never once been checked against an
exported trace. Now it is, by `tests/test_langfuse_live.py`.

## The trap

Every one of these was **a green result that was not evidence** — the recurring
lesson of this log, at product scale. The e2e added its own instance while closing
them: rebuilding an image under a fixed tag makes the Deployment spec byte-identical,
so helm performs no rollout and the smoke test exercises *last* run's code. It
401'd against a key it had just minted, because the pods were still running the
build that read `VINEA_API_KEYS`.

## Read this

- [ADR-012](../adr/012-api-keys-in-the-database.md) — why SHA-256 and not bcrypt,
  and why the access log is not two more columns on the latency table.
- The [ADR-011 amendment](../adr/011-lexical-retrieval-only.md) — the change that
  looks principled scoring best on the questions its author wrote, caught twice.
- `infra/kind-e2e.sh` — every gap above has an assertion here that fails without
  its fix.

## Try it

```bash
uv run python scripts/measure_retrieval.py --misses   # re-derive every retrieval number
python -m vinea.keys issue --tenant acme --label t    # mint; then revoke and watch the 401
python -m vinea.slo check --no-notify                 # measure without telling the channel
```
