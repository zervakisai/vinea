"""Global test config.

Two things live here: the guard that forbids any real model request (so tests
never hit a live LLM -- overrides with TestModel/FunctionModel bypass it), and
the database fixtures (phase 6).
"""

from __future__ import annotations

import os
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
    from sqlmodel import create_engine

    url = _test_database_url()
    if not url:
        pytest.skip(
            "No VINEA_TEST_DATABASE_URL/DATABASE_URL set. "
            "Start Postgres with `docker compose up -d postgres` and copy .env.example to .env."
        )

    engine = create_engine(url)
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
