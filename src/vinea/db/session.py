"""Engine and session construction. Config, not logic.

Sync SQLAlchemy on psycopg3, deliberately. The workload that touches this is
either a worker handling one task at a time (S3) or a thin read/enqueue API
(S5); neither is served by the complexity of async sessions, and the thing
those workers actually wait on is the model API, which pydantic-ai already
awaits. `asyncio.run(run_advisory(...))` inside a sync worker is the same shape
`graph.run_advisory_sync` already uses for the CLI.
"""

from __future__ import annotations

import os

from sqlalchemy import Engine
from sqlmodel import Session, create_engine

# The compose stack publishes 55433 on the host: 5432 collides with a system
# Postgres, and 55432 is taken by a sibling project -- see docker-compose.yml.
DEFAULT_DATABASE_URL = "postgresql+psycopg://vinea:vinea@localhost:55433/vinea"


# The role the policies actually apply to. A `SET ROLE` on connection checkout
# rather than a second DATABASE_URL: one connection string, one pool, and every
# query runs as a role that is NOSUPERUSER and NOBYPASSRLS.
#
# This exists because the first version of the RLS migration did not have it and
# was completely inert. `vinea` is the container's bootstrap role -- superuser,
# BYPASSRLS -- and superusers bypass row security unconditionally, FORCE
# included. Policies applied, `rowsecurity` reported true, and every scoped
# session still read every tenant's rows.
APP_ROLE = "vinea_app"


def database_url() -> str:
    """The URL to connect to, from the environment.

    A pure function of `os.environ`, like `config`'s API-key probe -- no
    caching, so a test can monkeypatch the environment and get an honest answer.
    """
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Build an Engine. Callers own its lifetime; nothing here is global.

    No module-level engine on purpose: a process-wide singleton created at
    import time is what makes tests need monkeypatching and makes a worker that
    forks inherit a connection it must not share.
    """
    engine = create_engine(url or database_url(), echo=echo, pool_pre_ping=True)
    _restrict_role_on_connect(engine)
    return engine


def _restrict_role_on_connect(engine: Engine) -> None:
    """Every connection this engine hands out runs as the restricted role.

    On checkout rather than per query, and that is what makes the isolation
    *fail closed* instead of fail-if-remembered. With the role applied only
    inside `scope_to_tenant`, a session that forgot to scope itself would run as
    the bootstrap superuser and see everything -- which replaces "29 places to
    forget a WHERE" with "one place to forget a call", an improvement rather
    than a guarantee.

    Applied here, a session that declares no tenant sees NOTHING: it is already
    the restricted role, `current_setting('vinea.tenant', true)` is NULL, and
    the policy filters every row. Forgetting is now the safe direction.

    Alembic is unaffected -- `migrations/env.py` builds its own engine with
    `engine_from_config` and never calls this function, so DDL still runs as the
    owner. That separation is load-bearing: a migration running as `vinea_app`
    could not create a table, and the failure would be baffling.
    """
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_role(dbapi_connection, _record):  # pragma: no cover - driver callback
        # COMMIT is load-bearing, and its absence cost an hour.
        #
        # `SET ROLE` is TRANSACTIONAL in Postgres. Issued inside the implicit
        # transaction this cursor opens, it is undone by the ROLLBACK SQLAlchemy
        # performs when the connection returns to the pool -- so the very first
        # `engine.connect()` reported `vinea_app` and every session after it
        # reported `vinea`. Half the suite would have run restricted and half
        # unrestricted, depending on pool reuse, which is a worse failure than
        # not doing it at all because it is intermittent.
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"SET ROLE {APP_ROLE}")
        dbapi_connection.commit()


def make_session(engine: Engine) -> Session:
    """One session. Use as a context manager."""
    return Session(engine)


# ---------------------------------------------------------------------------
# Row-level security scoping
# ---------------------------------------------------------------------------
#
# After migration f92c4d1a7b60 every tenant-scoped table carries a policy of the
# form `tenant = current_setting('vinea.tenant', true) OR
# current_setting('vinea.ops', true) = 'on'`. A connection that declares neither
# sees nothing at all -- `current_setting(..., true)` returns NULL, `tenant =
# NULL` is NULL, and the row is filtered out.
#
# `SET LOCAL`, not `SET`. The distinction is the entire reason this is safe with
# a shared connection pool: LOCAL scopes the setting to the current transaction,
# so it is discarded at COMMIT or ROLLBACK and cannot ride a pooled connection
# into the next request. A plain `SET` would leak one tenant's scope into
# whoever borrows that connection next, which is a worse bug than the one RLS
# was added to prevent.


# Where a session's declared scope is remembered, and why it has to be.
#
# `SET LOCAL` is discarded at COMMIT -- that is exactly the property that makes
# it safe on a pooled connection, and it is also a trap for any session that
# commits and keeps working. The worker does precisely that: `process_one`
# commits the advisory and its task, then `run_worker` loops and claims again.
# Without re-application the second claim runs unscoped, sees nothing, and the
# symptom surfaces far from the cause as "could not refresh instance".
#
# So the scope is recorded as an *intent* on the session and re-applied at the
# start of every transaction. Declaring it once then stays true for the session's
# whole life, which is what every caller already assumed.
_SCOPE_KEY = "vinea_scope"


def _apply_scope(executor, scope) -> None:
    """Issue the scoping statements on a Session or a Connection.

    Takes an executor rather than a Session because the `after_begin` hook must
    use the *connection* it is handed: calling `session.execute` from inside the
    session's own begin is reentrant and SQLAlchemy refuses it with "this session
    is provisioning a new connection". Same two statements, two callers, one
    function.
    """
    from sqlalchemy import text

    if scope is None:
        return
    kind, value = scope
    executor.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    if kind == "tenant":
        executor.execute(
            text("SELECT set_config('vinea.tenant', :tenant, true)"), {"tenant": value}
        )
    else:
        executor.execute(text("SELECT set_config('vinea.ops', 'on', true)"))


def _remember_scope(session: Session, kind: str, value: str | None) -> None:
    session.info[_SCOPE_KEY] = (kind, value)
    if not _listener_installed(session):
        from sqlalchemy import event

        @event.listens_for(session, "after_begin")
        def _reapply(sess, _transaction, connection):  # pragma: no cover - ORM callback
            _apply_scope(connection, sess.info.get(_SCOPE_KEY))

        session.info["vinea_scope_listener"] = True
    _apply_scope(session, session.info[_SCOPE_KEY])


def _listener_installed(session: Session) -> bool:
    return bool(session.info.get("vinea_scope_listener"))


def scope_to_tenant(session: Session, tenant: str) -> None:
    """Restrict this transaction to one tenant's rows.

    Parameterised via `set_config` rather than string-formatted into a `SET
    LOCAL` statement. `SET LOCAL x = $1` is not valid SQL -- the value must be a
    literal -- so the naive version interpolates the tenant into the statement
    text, which is the one place in this codebase where a tenant name would
    become executable SQL. `set_config(name, value, is_local)` takes bind
    parameters and is the same thing without that property.
    """
    _remember_scope(session, "tenant", tenant)


def scope_to_ops(session: Session) -> None:
    """Allow this transaction to see every tenant.

    For the worker, which claims from a queue spanning all tenants, and for the
    `/ops/*` endpoints, which aggregate across them by design. Both are
    legitimately cross-tenant; neither is reachable with a tenant API key.

    This is the escape hatch the policy grants, and its existence is what makes
    this defence "you cannot forget a filter" rather than "you cannot read
    another tenant". ADR-009 says so plainly and records the stronger version
    (a second database role with no escape) as the revisit trigger.
    """
    # Ops still runs as the restricted role. Running ops queries as the bootstrap
    # superuser would work, and would mean the one path that legitimately crosses
    # tenants is the one path never subject to the policy -- so a bug in the
    # policy would hide exactly there.
    _remember_scope(session, "ops", None)
