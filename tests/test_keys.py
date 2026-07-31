"""Keys in the database: hashed, revocable, and logged when used.

Two properties carry this file, and each has failed silently somewhere in this
project's history in a way that reported success.

**The stored form must not be the credential.** A test that only checks "the right
key authenticates" passes just as happily against a table full of plaintext. So
several tests read the raw rows and assert the secret is *not* there.

**Revocation must take effect on the next request, not the next deploy.** That was
the whole reason for the table, and the way to get it wrong is a cache: a mapping
loaded once at import would keep a revoked key working for the life of the process,
and every test that revokes and re-checks within one session would still pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import select

from tests.conftest import open_ops_session
from vinea import keys
from vinea.api import main
from vinea.db.models import AccessLog, ApiKey

pytestmark = pytest.mark.db

TENANT = "acme"


# --------------------------------------------------------------------------- #
# Minting and storage                                                          #
# --------------------------------------------------------------------------- #


def test_the_key_is_not_in_the_database(committing_db):
    """The point of the whole change, asserted against the raw row.

    Not "a wrong key is rejected" -- that passes against plaintext storage too.
    This reads every column of the row and requires the secret to appear in none
    of them.
    """
    with open_ops_session(committing_db) as session:
        issued = keys.issue(session, tenant=TENANT, label="storage test")
        session.commit()
        secret = issued.secret

        row = session.execute(text("SELECT * FROM api_keys")).mappings().one()

    for column, value in row.items():
        assert secret != value, f"the key itself is stored in {column}"
        if isinstance(value, str):
            assert secret not in value, f"the key is embedded in {column}"

    assert row["key_hash"] == keys.hash_key(secret)
    assert len(row["key_hash"]) == 64  # sha256 hex


def test_the_prefix_identifies_without_authenticating(committing_db):
    """Enough of the key to find the row; never enough to use it."""
    with open_ops_session(committing_db) as session:
        issued = keys.issue(session, tenant=TENANT, label="prefix test")
        session.commit()

    assert issued.secret.startswith(issued.prefix)
    assert len(issued.prefix) < len(issued.secret) / 2 + 1
    # Greppable: a secret scanner can find this in a commit or a paste.
    assert issued.secret.startswith("vinea_t_")


def test_a_short_legacy_key_is_not_stored_whole_as_its_own_prefix(committing_db):
    """The degenerate case that inverts the design if the prefix is a fixed slice.

    `import-env` takes over keys like `key-acme` -- eight characters. A fixed
    20-character prefix would copy the entire credential into the column whose
    purpose is to hold something that is not the credential, in the same table, in
    the clear. The half rule is what stops that.
    """
    assert keys.prefix_of("key-acme") == "key-"
    assert keys.prefix_of("short") == "shor"
    minted = keys.generate_key(keys.TENANT_SCOPE)
    assert keys.prefix_of(minted) == minted[:20]


def test_an_ops_key_carries_no_tenant_and_the_database_enforces_it(committing_db):
    """The check constraint, not just the Python guard.

    A tenant key with no tenant would authenticate to nothing; an ops key with one
    would imply a scoping that does not exist. Both are refused by the database, so
    a future caller that bypasses `issue` cannot create either.
    """
    with open_ops_session(committing_db) as session:
        issued = keys.issue(session, tenant=None, scope=keys.OPS_SCOPE, label="ops")
        session.commit()
        assert issued.row.tenant is None

        with pytest.raises(Exception, match="ck_api_keys_scope_tenant"):
            session.execute(
                text(
                    "INSERT INTO api_keys (tenant, scope, label, prefix, key_hash) "
                    "VALUES ('acme', 'ops', 'illegal', 'p1', 'h1')"
                )
            )
            session.commit()
        session.rollback()


def test_issue_refuses_a_key_nobody_can_identify(committing_db):
    with open_ops_session(committing_db) as session:
        with pytest.raises(ValueError, match="label"):
            keys.issue(session, tenant=TENANT, label="   ")
        with pytest.raises(ValueError, match="needs a tenant"):
            keys.issue(session, tenant=None, label="no tenant")
        with pytest.raises(ValueError, match="must not name one"):
            keys.issue(session, tenant=TENANT, scope=keys.OPS_SCOPE, label="both")


# --------------------------------------------------------------------------- #
# Verification: the reason, not just the verdict                               #
# --------------------------------------------------------------------------- #


def test_verify_distinguishes_unknown_revoked_and_expired(committing_db):
    """Three failures that look identical to a caller and demand different responses.

    A misconfigured client, a credential that should already be dead being used
    again, and a rotation nobody finished. The API returns the same 401 for all
    three -- distinguishing them for the caller would help a prober -- and the log
    keeps them apart.
    """
    with open_ops_session(committing_db) as session:
        live = keys.issue(session, tenant=TENANT, label="live")
        dead = keys.issue(session, tenant=TENANT, label="revoked")
        stale = keys.issue(
            session,
            tenant=TENANT,
            label="expired",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        keys.revoke(session, prefix=dead.prefix)
        session.commit()

        assert keys.verify(session, live.secret).outcome == keys.OK
        assert keys.verify(session, dead.secret).outcome == keys.REVOKED
        assert keys.verify(session, stale.secret).outcome == keys.EXPIRED
        assert keys.verify(session, "vinea_t_nonsense").outcome == keys.UNKNOWN_KEY
        assert keys.verify(session, None).outcome == keys.NO_KEY
        assert keys.verify(session, "").outcome == keys.NO_KEY


def test_a_tenant_key_is_not_an_ops_key(committing_db):
    """Scope is checked, and the mismatch is its own outcome.

    A tenant key turning up on the operator surface is a different event from a
    typo, so it is recorded as one.
    """
    with open_ops_session(committing_db) as session:
        tenant_key = keys.issue(session, tenant=TENANT, label="tenant")
        ops_key = keys.issue(session, tenant=None, scope=keys.OPS_SCOPE, label="ops")
        session.commit()

        assert keys.verify(session, tenant_key.secret, scope=keys.OPS_SCOPE).outcome == (
            keys.WRONG_SCOPE
        )
        assert keys.verify(session, ops_key.secret, scope=keys.TENANT_SCOPE).outcome == (
            keys.WRONG_SCOPE
        )
        assert keys.verify(session, ops_key.secret, scope=keys.OPS_SCOPE).ok


def test_last_used_is_recorded_but_not_on_every_call(committing_db):
    """Useful enough to answer "can we retire this", cheap enough for the read path.

    An UPDATE per request would put a write on the grower-facing read to sharpen a
    timestamp nobody reads at that resolution.
    """
    with open_ops_session(committing_db) as session:
        issued = keys.issue(session, tenant=TENANT, label="touch")
        session.commit()
        assert issued.row.last_used_at is None

        keys.verify(session, issued.secret)
        session.commit()
        first = issued.row.last_used_at
        assert first is not None

        keys.verify(session, issued.secret)
        session.commit()
        assert issued.row.last_used_at == first, "a second call inside the window rewrote it"

        # Push it outside the window and it advances.
        issued.row.last_used_at = datetime.now(UTC) - timedelta(hours=2)
        session.commit()
        keys.verify(session, issued.secret)
        session.commit()
        assert issued.row.last_used_at > first


def test_revoking_twice_is_not_an_error(committing_db):
    """Revoking twice is what a worried operator does. It must not look like failure."""
    with open_ops_session(committing_db) as session:
        issued = keys.issue(session, tenant=TENANT, label="twice")
        session.commit()

        first = keys.revoke(session, prefix=issued.prefix)
        session.commit()
        assert first is not None and first.revoked_at is not None
        stamp = first.revoked_at

        again = keys.revoke(session, prefix=issued.prefix)
        session.commit()
        assert again is not None
        assert again.revoked_at == stamp, "the second revoke moved the timestamp"

        assert keys.revoke(session, prefix="vinea_t_nosuchkey") is None


def test_a_revoked_key_keeps_its_row_and_its_history(committing_db):
    """Revocation is a column, not a DELETE.

    A deleted row makes a returning key look *unknown*, which is a different and
    much less alarming event than a key that was explicitly killed being used
    again.
    """
    with open_ops_session(committing_db) as session:
        issued = keys.issue(session, tenant=TENANT, label="history")
        session.commit()
        keys.revoke(session, prefix=issued.prefix)
        session.commit()

        rows = session.exec(select(ApiKey)).all()
        assert len(rows) == 1
        assert rows[0].label == "history"
        assert keys.verify(session, issued.secret).outcome == keys.REVOKED


# --------------------------------------------------------------------------- #
# Through the API                                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(committing_db):
    main.app.dependency_overrides[main.get_engine] = lambda: committing_db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _issue(engine, **kwargs) -> str:
    with open_ops_session(engine) as session:
        issued = keys.issue(session, **kwargs)
        session.commit()
        return issued.secret


def test_revocation_takes_effect_on_the_very_next_request(api, committing_db):
    """The property the table exists to provide, end to end.

    Under `VINEA_API_KEYS` this took a Secret edit and a rolling restart. If a
    future refactor caches the key mapping at import -- the obvious optimisation --
    this test is what catches it.
    """
    secret = _issue(committing_db, tenant=TENANT, label="revocation")

    assert api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": secret}).status_code == 404

    with open_ops_session(committing_db) as session:
        keys.revoke(session, prefix=keys.prefix_of(secret))
        session.commit()

    r = api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": secret})
    assert r.status_code == 401, "a revoked key still worked; something is caching"


def test_an_expired_key_stops_working_without_anyone_revoking_it(api, committing_db):
    secret = _issue(
        committing_db,
        tenant=TENANT,
        label="expiring",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    r = api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": secret})
    assert r.status_code == 401


def test_the_401_does_not_say_which_kind_of_invalid(api, committing_db):
    """Telling a caller "expired" rather than "unknown" confirms the key existed."""
    secret = _issue(committing_db, tenant=TENANT, label="quiet")
    with open_ops_session(committing_db) as session:
        keys.revoke(session, prefix=keys.prefix_of(secret))
        session.commit()

    revoked = api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": secret})
    unknown = api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": "vinea_t_no"})
    missing = api.get(f"/advisories/{TENANT}/2025-02-08")

    assert revoked.status_code == unknown.status_code == missing.status_code == 401
    assert revoked.json()["detail"] == unknown.json()["detail"] == missing.json()["detail"]


# --------------------------------------------------------------------------- #
# The access log                                                               #
# --------------------------------------------------------------------------- #


def _log(engine) -> list[AccessLog]:
    with open_ops_session(engine) as session:
        return list(session.exec(select(AccessLog).order_by(AccessLog.id)).all())


def test_a_successful_call_is_logged_with_the_status_it_actually_returned(api, committing_db):
    """Logged from the middleware, not the dependency, and this is why.

    A dependency runs before the route and only knows "authenticated: yes". The
    interesting pattern -- one key asking for things that do not exist, over and
    over -- is a 404 pattern, and it is invisible unless the status is recorded.
    """
    secret = _issue(committing_db, tenant=TENANT, label="logged")
    api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": secret})

    rows = _log(committing_db)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.tenant == TENANT
    assert entry.outcome == keys.OK
    assert entry.status_code == 404, "the log recorded authentication, not the outcome"
    assert entry.route == "/advisories/{tenant}/{run_date}", "a resolved path leaks the tenant"
    assert entry.key_prefix == keys.prefix_of(secret)


def test_a_rejection_is_logged_with_the_reason(api, committing_db):
    """`status_code` says it failed; `outcome` says how the credential failed."""
    secret = _issue(committing_db, tenant=TENANT, label="rejected")
    with open_ops_session(committing_db) as session:
        keys.revoke(session, prefix=keys.prefix_of(secret))
        session.commit()

    api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": secret})
    api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": "vinea_t_unknown"})
    api.get(f"/advisories/{TENANT}/2025-02-08")

    outcomes = [row.outcome for row in _log(committing_db)]
    assert outcomes == [keys.REVOKED, keys.UNKNOWN_KEY, keys.NO_KEY]


def test_a_key_used_against_another_tenant_is_logged_as_such(api, committing_db):
    """The event worth alerting on, and it must not be filed as a generic 403."""
    secret = _issue(committing_db, tenant="olivares", label="wandering")
    r = api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": secret})
    assert r.status_code == 403

    rows = _log(committing_db)
    assert [row.outcome for row in rows] == [keys.WRONG_TENANT]
    # The key that did it is identified, and the tenant recorded is the key's own --
    # the row says "olivares's key went looking at acme", which is the useful shape.
    assert rows[0].key_prefix == keys.prefix_of(secret)
    assert rows[0].tenant == "olivares"


def test_an_unknown_key_belongs_to_nobody_and_is_visible_only_to_ops(committing_db, api):
    """A NULL tenant is invisible under every tenant scope, by arithmetic.

    `tenant = 'acme'` is NULL for a NULL tenant, so the policy filters it -- no
    explicit check, and nothing to forget. A rejected credential is an operator's
    business, not a tenant's.
    """
    from sqlmodel import Session

    from vinea.db.session import scope_to_tenant

    api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": "vinea_t_nobody"})

    with open_ops_session(committing_db) as session:
        assert len(session.exec(select(AccessLog)).all()) == 1

    with Session(committing_db) as session:
        scope_to_tenant(session, TENANT)
        assert session.exec(select(AccessLog)).all() == []


def test_the_access_log_is_isolated_between_tenants(committing_db, api):
    """Two tenants, two calls, and neither can see the other's row."""
    from sqlmodel import Session

    from vinea.db.session import scope_to_tenant

    acme = _issue(committing_db, tenant=TENANT, label="acme")
    olivares = _issue(committing_db, tenant="olivares", label="olivares")
    api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": acme})
    api.get("/advisories/olivares/2025-02-08", headers={"X-API-Key": olivares})

    with open_ops_session(committing_db) as session:
        assert len(session.exec(select(AccessLog)).all()) == 2

    for tenant in (TENANT, "olivares"):
        with Session(committing_db) as session:
            scope_to_tenant(session, tenant)
            rows = session.exec(select(AccessLog)).all()
            assert [row.tenant for row in rows] == [tenant], f"{tenant} saw somebody else's calls"


def test_the_api_keys_table_is_not_readable_from_a_tenant_scope(committing_db):
    """Defence in depth: no route reads this table, and if one ever does it is policed.

    The lookup itself runs under the ops escape because it happens before the tenant
    is known. That ordering is unavoidable in any credential system; what is
    avoidable is a *tenant-facing* query reaching the table, and the policy is what
    makes that impossible rather than merely unwritten.
    """
    from sqlmodel import Session

    from vinea.db.session import scope_to_tenant

    _issue(committing_db, tenant=TENANT, label="policed")
    _issue(committing_db, tenant="olivares", label="other")

    with Session(committing_db) as session:
        scope_to_tenant(session, TENANT)
        rows = session.exec(select(ApiKey)).all()
        assert [row.tenant for row in rows] == [TENANT]


def test_the_latency_middleware_writes_where_the_test_pointed_the_app(
    api, committing_db, monkeypatch
):
    """The SLI's collection path, exercised through the app for the first time.

    Not about keys, and here because this work is what surfaced it. Every existing
    test that asserts on `api_request_samples` inserts the rows itself, so the
    middleware that produces them in production had never run under test -- and it
    swallows its own failures into a `debug` log, so breaking it would have shown up
    as the objective reporting "no data" rather than as anything red.

    It also reached the database through `get_engine()`, which ignores
    `app.dependency_overrides`. That is masked whenever `VINEA_TEST_DATABASE_URL`
    and `DATABASE_URL` name the same database, which is the normal local and CI
    setup -- so it is latent rather than currently breaking, and it is the kind of
    latent that surfaces the first time someone points a test at a scratch database.
    Which is why `make_engine` is sabotaged below. With it removed, anything that
    builds its own engine instead of taking the override fails -- so this test
    distinguishes "wrote a row" from "wrote a row to the database we asked for",
    which two identical URLs otherwise make indistinguishable.
    """
    from vinea.db.models import ApiRequestSample

    assert main.current_engine() is committing_db, (
        "code outside the dependency graph is not honouring the test's override"
    )
    # No path out of this process except the override. `_engine` is reset so the
    # cached one cannot stand in for it.
    monkeypatch.setattr(main, "_engine", None)
    monkeypatch.setattr(
        main, "make_engine", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not the override"))
    )

    secret = _issue(committing_db, tenant=TENANT, label="timed")
    api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": secret})

    with open_ops_session(committing_db) as session:
        samples = session.exec(select(ApiRequestSample)).all()

    assert len(samples) == 1, "the timing went to a different database, or nowhere"
    assert samples[0].route == "/advisories/{tenant}/{run_date}"
    assert samples[0].duration_ms > 0


def test_the_audit_write_cannot_break_the_request(api, committing_db, monkeypatch):
    """A stated limit, tested rather than trusted.

    An audit trail that can 500 a grower's morning read has inverted its own
    priority. The consequence -- the log may silently drop rows, so an empty log is
    not proof an attempt did not happen -- is recorded in ADR-012.

    Getting the injection point right took two attempts, and both failures were
    instructive. Patching `_record` itself removed the protection and then reported
    it missing -- the `try` lives inside that function. Patching `_engine` broke
    *authentication* too, which must fail closed, so a 401 was the correct answer
    and the test was asserting the wrong one.

    Breaking the row construction is the narrow cut: authentication still works, the
    audit write does not. It also caught two statements sitting outside the `try`,
    where a failure would have escaped into a 500.
    """
    secret = _issue(committing_db, tenant=TENANT, label="resilient")

    from vinea.db import models

    def broken(*args, **kwargs):
        raise RuntimeError("the access_log table is gone")

    monkeypatch.setattr(models, "AccessLog", broken)
    r = api.get(f"/advisories/{TENANT}/2025-02-08", headers={"X-API-Key": secret})
    assert r.status_code == 404, "a broken audit path changed what the grower got"

    monkeypatch.undo()
    assert _log(committing_db) == [], "the row landed anyway; the test proved nothing"
