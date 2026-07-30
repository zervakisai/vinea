"""api_request_samples: the read-latency SLI's raw data

Revision ID: f5c81b30e2a7
Revises: d8a3f1207c94
Create Date: 2026-07-30 11:02:18.442901

ADR-010 declared a read-latency objective and left it uncollected, because
latency was not persisted anywhere and the alternatives were a process-local
histogram (dies with the pod) or a metrics backend this project declined to run.

This is the third option, available because of the traffic profile: a
nightly-advisory product serves the grower-facing read a few hundred times a day,
so storing every timing is thousands of rows a week. At that size an exact
`percentile_cont(0.95)` is simpler than bucket boundaries and keeps the SLI a SQL
query.

No `tenant` column, therefore no row policy. The SLO is fleet-wide, and `route`
holds the template rather than the resolved path -- both so the series is
aggregatable and so tenant names stay out of a table nothing is policing.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5c81b30e2a7"
down_revision: Union[str, Sequence[str], None] = "d8a3f1207c94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_request_samples",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_api_request_samples_route_time", "api_request_samples", ["route", "observed_at"]
    )
    # ALTER DEFAULT PRIVILEGES in f92c4d1a7b60 covers tables created after it, so
    # `vinea_app` can already write here. Asserted rather than assumed: without the
    # grant every request would fail on a permission error the API catches and
    # logs at DEBUG, and latency would silently never be recorded.
    op.execute("GRANT SELECT, INSERT ON api_request_samples TO vinea_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE api_request_samples_id_seq TO vinea_app")


def downgrade() -> None:
    op.drop_index("ix_api_request_samples_route_time", table_name="api_request_samples")
    op.drop_table("api_request_samples")
