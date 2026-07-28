"""Alembic environment.

Three edits from the generated template, all load-bearing:

  1. `target_metadata` points at SQLModel's registry, and importing
     `vinea.db.models` is what populates it. Without that import the metadata is
     empty and `alembic revision --autogenerate` cheerfully writes a migration
     that drops every table -- the classic autogenerate footgun.
  2. The URL comes from the environment, not from alembic.ini. Credentials do
     not belong in a committed file, and the same migrations have to run against
     local compose, a test database, and production without an edit.
  3. `compare_type=True`, so autogenerate notices a column *changing* type and
     not just appearing. Off by default, which is surprising exactly once.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

# Imported for the side effect that matters: registering every table on
# SQLModel.metadata. Do not "clean up" this import -- see note 1 above.
import vinea.db.models  # noqa: F401
from vinea.db.session import database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Precedence: whatever a programmatic caller set on the Config (the test fixture
# does this, pointing at its own database), then $DATABASE_URL, then the compose
# default. alembic.ini itself ships this empty, so the CLI path lands on
# $DATABASE_URL.
config.set_main_option(
    "sqlalchemy.url",
    config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL") or database_url(),
)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting ("alembic upgrade head --sql")."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
