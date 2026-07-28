"""Multi-tenancy: keep tenants' costs apart the way their data already is.

DESIGN.md B1: more than one grower, more than one org, means costs kept separate
the same way data is -- a budget per tenant so one noisy sensor feed can't starve
everyone else's quota, a cache namespaced by tenant so one grower's numbers never
leak into another's lookup, and region as a config value on the tenant record
(already done: `grower_config.region`, phase 6).

Crucially, none of this changes what a *single* day's advisory computation looks
like. It's entirely about how many run at once and who pays for what. That's why
this is a small module beside the worker rather than a change to the graph or the
agents.

The anti-pattern this file exists to avoid is the one DESIGN.md calls out:
semantic caching. Close-enough inputs do NOT mean close-enough decisions here -- a
depletion of 67.4mm and 67.6mm sit a hair apart in any embedding space and on
opposite sides of the irrigation trigger. So the cache key is an exact
deterministic fingerprint (tenant + run_date + deps_hash), never a similarity
match. The `feature_cache` table already keys exactly this way; this module is the
tenant-namespacing discipline around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlmodel import Session, select

from vinea.db.mapping import deps_hash
from vinea.db.models import FeatureCache
from vinea.deps import Deps

# A conservative default per-tenant ceiling on model calls per nightly run. One
# tenant's runaway (a sensor feed spewing borderline days) can burn its own budget
# to zero and be throttled, without touching anyone else's.
DEFAULT_TENANT_CALL_BUDGET = 100


@dataclass(frozen=True, slots=True)
class TenantBudget:
    """A per-tenant ceiling on model calls, with a running count.

    Deliberately a plain in-memory tally rather than a distributed rate limiter:
    within one nightly run the worker fleet is small and the point is isolation
    between tenants, not precise global accounting. The lesson is the *shape* --
    budget is per-tenant, so one tenant hitting its ceiling fails fast for that
    tenant alone -- not the mechanism, which a production system would back with
    Redis or a token bucket.

    Maps onto pydantic-ai's own `UsageLimits`: when the worker runs the graph, it
    can pass `UsageLimits(request_limit=budget.remaining)` so the SDK enforces the
    ceiling at the call boundary too.
    """

    tenant: str
    limit: int = DEFAULT_TENANT_CALL_BUDGET
    spent: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def charge(self, n: int = 1) -> TenantBudget:
        """Return a new budget with `n` calls spent (frozen, so no mutation)."""
        return TenantBudget(tenant=self.tenant, limit=self.limit, spent=self.spent + n)


def cache_key(tenant: str, run_date: date, deps: Deps) -> tuple[str, date, str]:
    """The exact deterministic cache key: (tenant, run_date, deps_hash).

    Namespaced by tenant so one grower's features can never be served for
    another's request -- even if, by coincidence, two tenants had identical weather
    and config, the key keeps them separate, because a cache hit across a tenant
    boundary is a data-leak bug regardless of whether the numbers happen to match.

    Exact, never similarity: the deps_hash pins the thresholds, the run_date pins
    the day, and the tenant pins whose. There is no "close enough" here by design.
    """
    return (tenant, run_date, deps_hash(deps))


def get_cached_features(
    session: Session, *, tenant: str, run_date: date, deps: Deps
) -> dict | None:
    """Return cached feature JSON for this exact key, or None. Never cross-tenant.

    The cache is `feature_cache`, which ADR-001 labels disposable: a miss is always
    safe (recompute), and a stale entry can't exist because the key includes
    everything the features depend on -- change the config and the deps_hash
    changes, so you get a miss, not a wrong hit.
    """
    key_tenant, key_date, key_hash = cache_key(tenant, run_date, deps)
    row = session.exec(
        select(FeatureCache).where(
            FeatureCache.tenant == key_tenant,
            FeatureCache.run_date == key_date,
            FeatureCache.deps_hash == key_hash,
        )
    ).one_or_none()
    return row.features if row is not None else None


def put_cached_features(
    session: Session, *, tenant: str, run_date: date, deps: Deps, features: dict
) -> None:
    """Cache feature JSON under the exact tenant-namespaced key. Does not commit.

    Upsert on the composite primary key, so recomputing overwrites rather than
    conflicts. Safe to skip entirely -- it's a cache -- which is why nothing
    downstream treats a write failure here as fatal.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    key_tenant, key_date, key_hash = cache_key(tenant, run_date, deps)
    statement = (
        pg_insert(FeatureCache)
        .values(tenant=key_tenant, run_date=key_date, deps_hash=key_hash, features=features)
        .on_conflict_do_update(
            index_elements=["tenant", "run_date", "deps_hash"], set_={"features": features}
        )
    )
    session.execute(statement)
    session.flush()
