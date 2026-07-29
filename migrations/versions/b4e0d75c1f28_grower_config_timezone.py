"""grower_config: the grower's own morning

Revision ID: b4e0d75c1f28
Revises: f92c4d1a7b60
Create Date: 2026-07-29 18:44:02.913770

One additive nullable column, so the expand argument holds and this is one deploy.

The SLO is "an advisory by 06:00 LOCAL". A vineyard in Nemea and one in Mendoza
do not share a morning, and `grower_config` already carries `region` -- which is
a data-residency fact, not a clock. An IANA zone name rather than an offset:
offsets move twice a year and a stored one is wrong for half of it.

Nullable, with UTC applied on read rather than as a `server_default`. Existing
rows genuinely predate the column and we do not know where those growers are; a
fabricated local time would silently move every availability measurement. A
tenant judged against a UTC morning shows up as a breach and sends someone to fix
the config, which is the correct outcome -- better than being quietly excluded
from the numerator and the denominator both.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4e0d75c1f28"
down_revision: Union[str, Sequence[str], None] = "f92c4d1a7b60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("grower_config", sa.Column("timezone", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("grower_config", "timezone")
