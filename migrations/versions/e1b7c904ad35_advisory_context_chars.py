"""advisories: context_chars, the other half of the calibration pair

Revision ID: e1b7c904ad35
Revises: c73a51e8d4b2
Create Date: 2026-07-29 15:08:22.641907

One additive nullable column, so the same expand argument as a41f6b2c9e07 holds
and this is one deploy.

Why it exists rather than being computed: calibrating a chars-per-token estimate
needs **paired** numbers from the same request. `input_tokens` (phase 14) is one
half; without the other, dividing a token total by an estimate just returns the
assumption you were trying to check. `MeteredModel` records both at the one point
that sees the assembled request, so the two columns are NULL together and
populated together.

Not recomputable, therefore stored (ADR-001): the retrieved passages that made up
that night's prompt depend on a corpus that may since have been re-chunked. Same
argument as `advisory_citations.locator`.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1b7c904ad35"
down_revision: Union[str, Sequence[str], None] = "c73a51e8d4b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("advisories", sa.Column("context_chars", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("advisories", "context_chars")
