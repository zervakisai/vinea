"""citations survive a corpus re-ingest; record the model that served the call

Revision ID: d8a3f1207c94
Revises: b4e0d75c1f28
Create Date: 2026-07-30 09:14:52.007731

Two corrections.

**advisory_citations.chunk_id: CASCADE -> SET NULL.** `corpus_chunks` is a cache
and may be truncated or re-chunked at any time. Under CASCADE that deleted the
entire citation row, taking the denormalised `locator` with it -- the one field
added specifically so a citation stays readable after the index is rebuilt. The
column becomes nullable so the foreign key can be released without losing the
record of what was cited.

**advisories.served_model.** `model_id` records what was requested, which behind
a gateway is an alias. The alias does not identify a model a year later; this
column holds the concrete name when the provider reports one.

Both are safe under expand/contract: dropping a NOT NULL and adding a nullable
column are both compatible with code that predates them.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8a3f1207c94"
down_revision: Union[str, Sequence[str], None] = "b4e0d75c1f28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK = "advisory_citations_chunk_id_fkey"


def upgrade() -> None:
    op.alter_column("advisory_citations", "chunk_id", existing_type=sa.Integer(), nullable=True)
    op.drop_constraint(_FK, "advisory_citations", type_="foreignkey")
    op.create_foreign_key(
        _FK, "advisory_citations", "corpus_chunks", ["chunk_id"], ["id"], ondelete="SET NULL"
    )
    op.add_column("advisories", sa.Column("served_model", sa.Text(), nullable=True))


def downgrade() -> None:
    """Reverses the schema, and cannot reverse the data.

    Rows whose `chunk_id` has already been set to NULL by a corpus re-ingest
    cannot satisfy a NOT NULL constraint, so this deletes them -- which is the
    exact loss the upgrade exists to prevent. Forward-only in production; this
    body exists so a local branch can be unwound, and says plainly what it costs.
    """
    op.drop_column("advisories", "served_model")
    op.execute("DELETE FROM advisory_citations WHERE chunk_id IS NULL")
    op.drop_constraint(_FK, "advisory_citations", type_="foreignkey")
    op.create_foreign_key(
        _FK, "advisory_citations", "corpus_chunks", ["chunk_id"], ["id"], ondelete="CASCADE"
    )
    op.alter_column("advisory_citations", "chunk_id", existing_type=sa.Integer(), nullable=False)
