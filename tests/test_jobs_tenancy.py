"""phase 8 (S3.7) -- multi-tenancy: budgets kept apart, caches namespaced, no semantic match.

The budget tests are pure; the cache tests need Postgres (feature_cache is JSONB
with a composite key) and use the committing fixture.
"""

from __future__ import annotations

from datetime import date

import pytest

from vinea.deps import Deps
from vinea.jobs import tenancy

RUN_DATE = date(2025, 2, 8)


# --- per-tenant budget ------------------------------------------------------


def test_budget_isolates_one_tenants_spend_from_another():
    a = tenancy.TenantBudget(tenant="a", limit=5)
    a = a.charge(5)
    assert a.exhausted  # a is throttled...
    b = tenancy.TenantBudget(tenant="b", limit=5)
    assert not b.exhausted  # ...but b is untouched. That's the whole point.


def test_budget_is_frozen_and_charge_returns_a_new_value():
    a = tenancy.TenantBudget(tenant="a", limit=10)
    a2 = a.charge(3)
    assert a.spent == 0 and a2.spent == 3  # no mutation
    assert a2.remaining == 7


# --- namespaced, exact-match cache ------------------------------------------


def test_cache_key_is_namespaced_by_tenant():
    deps = Deps()
    key_a = tenancy.cache_key("tenant-a", RUN_DATE, deps)
    key_b = tenancy.cache_key("tenant-b", RUN_DATE, deps)
    # Same day, same config, different tenant -> different key. A cross-tenant hit
    # would be a data leak even if the numbers matched.
    assert key_a != key_b
    assert key_a[0] == "tenant-a"


def test_cache_key_changes_with_config_never_similarity():
    # 67.4 vs 67.6 depletion sit on opposite sides of the trigger; the point of
    # exact keying is that config (deps_hash) changes the key, not proximity.
    k1 = tenancy.cache_key("t", RUN_DATE, Deps())
    k2 = tenancy.cache_key("t", RUN_DATE, Deps(taw_mm=180.0))
    assert k1 != k2  # different config -> different key -> a miss, not a wrong hit


@pytest.mark.db
def test_cache_round_trip_and_tenant_isolation(committing_db):
    from sqlmodel import Session

    deps = Deps()
    feats = {"depletion": 120.0, "windows": 3}

    with Session(committing_db) as s:
        tenancy.put_cached_features(s, tenant="a", run_date=RUN_DATE, deps=deps, features=feats)
        s.commit()

    with Session(committing_db) as s:
        assert tenancy.get_cached_features(s, tenant="a", run_date=RUN_DATE, deps=deps) == feats
        # b misses, despite identical day and config. No cross-tenant serve.
        assert tenancy.get_cached_features(s, tenant="b", run_date=RUN_DATE, deps=deps) is None


@pytest.mark.db
def test_changing_config_is_a_miss_not_a_stale_hit(committing_db):
    from sqlmodel import Session

    with Session(committing_db) as s:
        tenancy.put_cached_features(
            s, tenant="a", run_date=RUN_DATE, deps=Deps(), features={"v": 1}
        )
        s.commit()

    with Session(committing_db) as s:
        # New config -> new deps_hash -> new key -> miss. A stale hit is impossible
        # because the key includes everything the features depend on.
        got = tenancy.get_cached_features(
            s, tenant="a", run_date=RUN_DATE, deps=Deps(taw_mm=180.0)
        )
        assert got is None
