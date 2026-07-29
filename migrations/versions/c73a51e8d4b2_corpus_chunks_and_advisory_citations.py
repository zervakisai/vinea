"""corpus_chunks (pgvector + FTS) and advisory_citations

Revision ID: c73a51e8d4b2
Revises: a41f6b2c9e07
Create Date: 2026-07-29 12:41:55.803114

The second schema change through phase 13's pre-upgrade hook, and unlike phase
14's it is not the easy case. Three things here can fail in ways an ADD COLUMN
cannot:

  **`CREATE EXTENSION vector` needs a database that has pgvector available.**
  The stock `postgres:16` image does not; `pgvector/pgvector:pg16` does, and
  managed Postgres offerings gate it behind a flag. `IF NOT EXISTS` makes the
  statement idempotent, not universally successful -- on a server without the
  extension files this migration fails, loudly, before any new pod serves
  traffic. That is the pre-upgrade hook doing its job.

  **The FTS index is on an expression.** `to_tsvector('english', text)` must
  match the query's expression character for character, or Postgres silently
  ignores the index and sequential-scans 800 rows while looking like it works.

  **There is no ANN index, deliberately.** See `upgrade()`.

Expand/contract still holds: both tables are new, so old code ignores tables it
has never heard of, and a rollback to the previous image is unaffected.
"""

from collections.abc import Sequence
from typing import Union

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c73a51e8d4b2"
down_revision: Union[str, Sequence[str], None] = "a41f6b2c9e07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the extension, the corpus table, and the citation table.

    **On the missing HNSW index.** Phase 13's `db_tier` variable carries a
    warning that `db-f1-micro` "is NOT enough for the phase-15 pgvector work,
    which will want more memory", and building an HNSW index is where that would
    bite. It is not built, because the corpus is 798 rows of 256 dimensions --
    about 800 KB of vectors. Postgres scans that exhaustively in single-digit
    milliseconds, which is exact rather than approximate and costs no build
    memory, no index maintenance, and no `ef_search` tuning constant.

    An ANN index earns its place somewhere north of 10^5 rows. Adding one here
    would be complexity bought with a guess (ADR-003's rule), and the honest
    version is a comment plus a measurement in the phase doc. The day a second
    corpus pushes this to six figures, the index is one migration -- and by then
    there will be a number to justify it.
    """
    # Idempotent, and required before the vector column type can be created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "corpus_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("chunk_id", sa.Integer(), nullable=False),
        sa.Column("chapter", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), server_default="", nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(dim=256), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "chunk_id", name="uq_corpus_chunks_natural"),
    )
    op.execute(
        "CREATE INDEX ix_corpus_chunks_fts ON corpus_chunks "
        "USING gin (to_tsvector('english', text))"
    )

    op.create_table(
        "advisory_citations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("advisory_id", sa.Integer(), nullable=False),
        sa.Column("leg", sa.Text(), nullable=False),
        sa.Column("chunk_id", sa.Integer(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "leg IN ('irrigation', 'spray', 'reconciliation')",
            name="ck_advisory_citations_leg",
        ),
        sa.ForeignKeyConstraint(["advisory_id"], ["advisories.id"], ondelete="CASCADE"),
        # ON DELETE CASCADE from a *cache* table, which looks wrong until you
        # read `advisory_citations.locator`: the text a reader needs is copied
        # onto the citation row, so truncating the corpus loses the link but not
        # the record of what was cited.
        sa.ForeignKeyConstraint(["chunk_id"], ["corpus_chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("advisory_id", "leg", "chunk_id", name="uq_advisory_citations_natural"),
    )
    op.create_index("ix_advisory_citations_advisory", "advisory_citations", ["advisory_id"])
    op.create_index("ix_advisory_citations_chunk", "advisory_citations", ["chunk_id"])


def downgrade() -> None:
    """Drop the tables. The extension stays.

    Dropping `vector` would break any *other* database object using it, and an
    extension is shared state that a single feature's migration has no business
    reclaiming. Same instinct as forward-only: a downgrade that reaches beyond
    what it created is a data-loss command wearing a migration's clothes.
    """
    op.drop_index("ix_advisory_citations_chunk", table_name="advisory_citations")
    op.drop_index("ix_advisory_citations_advisory", table_name="advisory_citations")
    op.drop_table("advisory_citations")
    op.execute("DROP INDEX IF EXISTS ix_corpus_chunks_fts")
    op.drop_table("corpus_chunks")
