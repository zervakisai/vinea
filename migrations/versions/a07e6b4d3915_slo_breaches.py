"""slo_breaches: how long, not just whether

Revision ID: a07e6b4d3915
Revises: f5c81b30e2a7
Create Date: 2026-07-30 11:41:07.556213

`python -m vinea.slo check` writes a row per unmet objective. This is history,
not alerting -- nothing notifies anyone, and ADR-010's refusal of a notification
path stands until somebody is on a rota.

What the table buys that a live query cannot is *duration*. Whether we are in
breach is answerable from the samples; how long we have been is not, because
`api_request_samples` is prunable and a missing advisory leaves no row at all. Nine
days in breach is a different conversation from nine hours.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "a07e6b4d3915"
down_revision: Union[str, Sequence[str], None] = "f5c81b30e2a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "slo_breaches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("target", sa.Float(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("budget_exhausted", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_slo_breaches_objective_time", "slo_breaches", ["objective", "detected_at"])
    op.execute("GRANT SELECT, INSERT ON slo_breaches TO vinea_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE slo_breaches_id_seq TO vinea_app")


def downgrade() -> None:
    op.drop_index("ix_slo_breaches_objective_time", table_name="slo_breaches")
    op.drop_table("slo_breaches")
