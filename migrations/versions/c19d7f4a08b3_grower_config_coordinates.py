"""grower_config: where the block actually is

Revision ID: c19d7f4a08b3
Revises: a07e6b4d3915
Create Date: 2026-07-30 14:22:09.331847

Two additive nullable columns, so the expand argument holds.

The worker loaded one weather file for every tenant. With a single demo site that
is invisible; with two tenants it is wrong -- identical advisories for vineyards
several hundred kilometres apart. `weather_observations` has been keyed by
(tenant, location) since it was created and `sources/persist.py` has always
written it correctly; nothing read it back, because nothing knew where a tenant
was.

Nullable rather than NOT NULL with a default: a fabricated coordinate is a
plausible-looking lie that would place a grower's vineyard somewhere it is not.
A tenant without coordinates falls back to the bundled capture and the advisory
carries a note saying so.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "c19d7f4a08b3"
down_revision: Union[str, Sequence[str], None] = "a07e6b4d3915"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("grower_config", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("grower_config", sa.Column("longitude", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("grower_config", "longitude")
    op.drop_column("grower_config", "latitude")
