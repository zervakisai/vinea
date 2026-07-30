"""Global test config.

Two things live here: the guard that forbids any real model request (so tests
never hit a live LLM -- overrides with TestModel/FunctionModel bypass it), and
the database fixtures (phase 6).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pydantic_ai.models
import pytest

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


# ---------------------------------------------------------------------------
# Database fixtures (phase 6)
# ---------------------------------------------------------------------------
#
# These SKIP rather than fail when no Postgres is reachable. The rest of the
# suite is pure-Python and offline, and it should stay runnable on a laptop with
# nothing installed -- a red suite that means "you didn't start Docker" trains
# people to ignore red. `pytest -m db` runs only these; CI starts the compose
# service and gets no skips.


def _test_database_url() -> str | None:
    return os.environ.get("VINEA_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
def db_engine():
    """A migrated engine against the test database, or a skip.

    Migrations run here rather than `SQLModel.metadata.create_all` on purpose: it
    makes these tests exercise the schema we actually ship, which is the only
    thing that catches a migration that diverges from the models.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.exc import OperationalError

    from vinea.db.session import make_engine

    url = _test_database_url()
    if not url:
        pytest.skip(
            "No VINEA_TEST_DATABASE_URL/DATABASE_URL set. "
            "Start Postgres with `docker compose up -d postgres` and copy .env.example to .env."
        )

    # `make_engine`, not `create_engine`: phase 17 attaches a connect-time
    # `SET ROLE vinea_app` there, and a test engine built without it runs as the
    # bootstrap superuser, which bypasses row-level security entirely. The suite
    # would then be green about a control it never exercises -- the same shape of
    # false evidence phase 13's e2e and phase 16's eval gate both produced.
    engine = make_engine(url)
    try:
        with engine.connect():
            pass
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable at {url}: {exc.__class__.__name__}")

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    yield engine
    engine.dispose()


@pytest.fixture
def ops_session(db_engine):
    """A rolled-back session allowed to see every tenant.

    Most tests are about behaviour that spans tenants (the queue, the scheduler,
    the ops endpoints) or seed rows for several. Under RLS that needs the
    cross-tenant escape, so it is a named fixture rather than something each test
    remembers to call -- and a test that wants isolation asks for `db_session`,
    which is scoped to nothing and therefore sees nothing until it says so.
    """
    from sqlmodel import Session

    from vinea.db.session import scope_to_ops

    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    scope_to_ops(session)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def db_session(db_engine):
    """One test, one transaction, rolled back at the end.

    Isolation by rollback rather than TRUNCATE: it's faster, and it means a
    failing test leaves the database exactly as it found it, so the next run
    isn't debugging the previous one's leftovers.
    """
    from sqlmodel import Session

    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# The queue tests (phase 8) need something the rollback fixture can't give: real
# COMMITs on separate connections. SKIP LOCKED is a claim about what two
# concurrent *transactions* do to each other, so it cannot be observed inside one
# transaction that never commits. `committing_db` yields the engine and guarantees
# a clean slate by TRUNCATE before and after, so each test starts from an empty
# queue and leaves nothing behind.
_ALL_TABLES = (
    "advisory_tasks, queue_depth_samples, eval_runs, annotations, "
    "advisories, grower_config, weather_observations, feature_cache, "
    # Added when the SLO work landed. A table missing from this list does not
    # fail -- it leaks rows into the next test, which shows up as an unrelated
    # assertion about a percentile computed over somebody else's samples.
    "api_request_samples, slo_breaches"
)


@pytest.fixture
def committing_db(db_engine):
    from sqlalchemy import text
    from sqlmodel import Session

    from vinea.db.session import APP_ROLE

    def _truncate():
        with Session(db_engine) as s:
            # RESET ROLE first: phase 17 puts every connection into `vinea_app`,
            # which deliberately has no TRUNCATE privilege. Cleaning the whole
            # database across every tenant is an administrative act, so it is
            # done as the owner -- explicitly, in one place, rather than by
            # granting the application a privilege it must never use.
            s.execute(text("RESET ROLE"))
            s.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
            # ...and back, BEFORE the commit. `SET ROLE` is transactional, so a
            # committed `RESET ROLE` sticks to this pooled connection for the
            # rest of its life and every test that borrows it afterwards runs as
            # the owner -- unrestricted, and green about a control it is no
            # longer exercising. That happened; this line is the fix.
            s.execute(text(f"SET ROLE {APP_ROLE}"))
            s.commit()

    _truncate()
    yield db_engine
    _truncate()


@contextmanager
def open_ops_session(engine):
    """A committing session allowed to see every tenant.

    The test-side counterpart of `jobs/worker.py`'s `scope_to_ops`. Tests that
    exercise the queue, the scheduler or cross-tenant storage behaviour are
    legitimately cross-tenant, exactly as the worker is -- so they declare it the
    same way rather than being handed an unrestricted connection.

    Using bare `Session(engine)` in a test now yields a session scoped to nothing,
    which after phase 17 sees nothing. That is the fixture doing its job: forgetting
    to declare scope fails loudly instead of quietly reading everything.
    """
    from sqlmodel import Session

    from vinea.db.session import scope_to_ops

    with Session(engine) as session:
        scope_to_ops(session)
        yield session
