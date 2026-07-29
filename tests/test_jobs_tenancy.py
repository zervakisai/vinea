"""Multi-tenancy: the feature cache is namespaced per tenant, and matched exactly.

A cache hit across a tenant boundary is a data leak whether or not the numbers
happen to coincide, so the key carries the tenant. It is also an exact
fingerprint rather than a similarity match: a depletion of 67.4 mm and 67.6 mm
sit a hair apart in any embedding space and on opposite sides of the irrigation
trigger.

These need Postgres -- `feature_cache` is JSONB with a composite key -- so they
use the committing fixture.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import open_ops_session
from vinea.deps import Deps
from vinea.jobs import tenancy

RUN_DATE = date(2025, 2, 8)


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

    deps = Deps()
    feats = {"depletion": 120.0, "windows": 3}

    with open_ops_session(committing_db) as s:
        tenancy.put_cached_features(s, tenant="a", run_date=RUN_DATE, deps=deps, features=feats)
        s.commit()

    with open_ops_session(committing_db) as s:
        assert tenancy.get_cached_features(s, tenant="a", run_date=RUN_DATE, deps=deps) == feats
        # b misses, despite identical day and config. No cross-tenant serve.
        assert tenancy.get_cached_features(s, tenant="b", run_date=RUN_DATE, deps=deps) is None


@pytest.mark.db
def test_changing_config_is_a_miss_not_a_stale_hit(committing_db):

    with open_ops_session(committing_db) as s:
        tenancy.put_cached_features(
            s, tenant="a", run_date=RUN_DATE, deps=Deps(), features={"v": 1}
        )
        s.commit()

    with open_ops_session(committing_db) as s:
        # New config -> new deps_hash -> new key -> miss. A stale hit is impossible
        # because the key includes everything the features depend on.
        got = tenancy.get_cached_features(
            s, tenant="a", run_date=RUN_DATE, deps=Deps(taw_mm=180.0)
        )
        assert got is None
