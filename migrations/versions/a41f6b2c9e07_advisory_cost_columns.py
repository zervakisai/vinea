"""advisories: cost evidence (input_tokens, output_tokens, cost_usd, cache_hit)

Revision ID: a41f6b2c9e07
Revises: 9dc180fddf7e
Create Date: 2026-07-29 09:12:04.118220

The first schema change since phase 13 built the pre-upgrade migration hook, and
deliberately the *easy* case: four additive, nullable columns with no backfill.

Expand/contract, spelled out for this migration:

    old code + new schema   ignores four columns it never selects.   safe
    new code + new schema   writes them.                             safe
    rollback to old image   old code, new schema, unharmed.          safe

Which is why it is one deploy rather than three. Saying so is the honest version
of "the mechanism works": this proves the plumbing, not the hard case. A rename or
a narrowing would need write-both / backfill / stop-reading-old across three
releases, and the day this project needs one, that is the phase that earns it.

No `server_default`. A default would make every advisory written before tonight
claim it cost zero -- a fabricated number in the one place the whole phase exists
to make trustworthy. NULL means "nobody was in the path who knew", which is
exactly the truth about every row already in the table.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a41f6b2c9e07"
down_revision: Union[str, Sequence[str], None] = "9dc180fddf7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the four cost columns. Additive, nullable, no backfill, no lock of note.

    `ADD COLUMN ... NULL` with no default is a catalogue-only change in Postgres
    11+: no table rewrite, no long ACCESS EXCLUSIVE lock, so this runs against a
    populated `advisories` in the pre-upgrade hook without holding the deploy.
    """
    op.add_column("advisories", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("advisories", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column("advisories", sa.Column("cost_usd", sa.Float(), nullable=True))
    op.add_column("advisories", sa.Column("cache_hit", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Drop them.

    Written because Alembic expects it and it is honest here -- these columns hold
    nothing that is not also on the gateway's own spend ledger, so dropping them
    loses attribution, not the money trail. That is NOT the general case: the
    production rule is forward-only, because a downgrade that drops a column with
    the only copy of something is a data-loss command wearing a migration's
    clothes. Rolling back code is `helm rollback`; rolling back schema is a new
    migration.
    """
    op.drop_column("advisories", "cache_hit")
    op.drop_column("advisories", "cost_usd")
    op.drop_column("advisories", "output_tokens")
    op.drop_column("advisories", "input_tokens")
