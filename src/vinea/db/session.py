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
    return create_engine(url or database_url(), echo=echo, pool_pre_ping=True)


def make_session(engine: Engine) -> Session:
    """One session. Use as a context manager."""
    return Session(engine)
