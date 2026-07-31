"""api keys in the database, and a log of who used them

Revision ID: a4e7b21c9053
Revises: c19d7f4a08b3
Create Date: 2026-07-31 09:14:02.881640

The fifth time this project trades a promise for a guarantee.

Before this, authentication was `VINEA_API_KEYS="key-acme:acme,key-olivares:olivares"`
-- every tenant's credential, in plaintext, in an environment variable readable by
anyone who could describe a pod. Revoking one meant editing a Secret and restarting
every process that read it, so between "this key is compromised" and "this key stops
working" sat a deploy.

Three properties arrive with the table, and only the table can provide them:

**Revocation is a row, not a rollout.** `UPDATE api_keys SET revoked_at = now()` and
the next request fails. No restart, no Secret edit, no window.

**The stored form is not the credential.** SHA-256, so `SELECT * FROM api_keys` --
or a leaked backup -- yields nothing that authenticates. The reason this is a plain
digest rather than bcrypt is in the model's docstring and worth reading: these are
32 random bytes, not passwords, so there is no dictionary to slow down and a slow
KDF would only add latency to the grower-facing read.

**Keys have a history.** Issued when, for what, last used when. "Which of these can
we retire" becomes a query rather than an argument.

## The access log is a second table on purpose

`api_request_samples` already records route, method, status and time. It records
**two GET routes**, deliberately, so liveness probes cannot drown the SLI. The
access log records **every authenticated call and every rejection**, including the
writes and `/ops/*`.

Same columns, different populations. Merging them would force one definition to
change and the one that changed would be silently wrong -- a latency percentile
whose denominator quietly grew to include health checks reads as an improvement.

## RLS, and the one table deliberately outside it

`access_log` carries a tenant and joins the row-policy set, so a tenant-facing view
of it is safe by construction rather than by a `WHERE` clause somebody remembers.
Rows from failed authentication have `tenant = NULL`; `NULL = 'acme'` is NULL, so
they are invisible to every tenant and visible under the ops escape -- which is
exactly right, since a rejected credential is an operator's business.

`api_keys` gets the same treatment, and it needs one extra thing: the policy allows
`tenant IS NULL` rows to be *written* under ops scope, because ops keys have no
tenant and the standard `WITH CHECK` would refuse them.

The lookup itself must run under ops scope, because it happens *before* the tenant
is known. That is not a hole in the isolation -- it is the same bootstrap ordering
every credential system has, and no tenant-facing route reads `api_keys`.

## Nothing is migrated automatically

This creates empty tables. Existing keys keep working until `VINEA_API_KEYS` is
removed, and `python -m vinea.keys import-env` stores their hashes so the keys
people already hold survive the cutover. A migration that minted credentials would
write them into a file every deploy replays, and everyone who can read the migration
history could then authenticate.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4e7b21c9053"
down_revision: Union[str, Sequence[str], None] = "c19d7f4a08b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirrors f92c4d1a7b60's policy, with one addition. `tenant IS NULL` rows -- ops
# keys, and log entries from a request that never authenticated -- must be
# writable and readable under the ops escape and invisible to every tenant.
# `tenant = current_setting(...)` already yields NULL (and therefore filters) for
# those rows on the read side; the explicit clause is what lets ops WRITE them,
# since a bare `WITH CHECK (tenant = ...)` refuses a NULL tenant outright.
_POLICY = """
CREATE POLICY tenant_isolation ON {table}
USING (
    tenant = current_setting('vinea.tenant', true)
    OR current_setting('vinea.ops', true) = 'on'
)
WITH CHECK (
    tenant = current_setting('vinea.tenant', true)
    OR current_setting('vinea.ops', true) = 'on'
)
"""

_TABLES = ("api_keys", "access_log")


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        # NULL for ops keys. The check constraint below is what keeps that from
        # meaning "a tenant key somebody forgot to fill in".
        sa.Column("tenant", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_hash"),
        sa.CheckConstraint(
            "(scope = 'tenant' AND tenant IS NOT NULL) OR (scope = 'ops' AND tenant IS NULL)",
            name="ck_api_keys_scope_tenant",
        ),
    )
    op.create_index("ix_api_keys_tenant", "api_keys", ["tenant"])
    # Not merely an index -- the uniqueness is the guarantee. Two rows sharing a
    # prefix would make `keys revoke <prefix>` ambiguous, and an ambiguous revoke
    # resolves to "revoked something" while the compromised key still works.
    op.create_index("uq_api_keys_prefix", "api_keys", ["prefix"], unique=True)

    op.create_table(
        "access_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant", sa.Text(), nullable=True),
        sa.Column("api_key_id", sa.Integer(), nullable=True),
        sa.Column("key_prefix", sa.Text(), nullable=True),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # SET NULL, not CASCADE: deleting a key must not erase the record of what it
        # did. `key_prefix` is denormalised so the row stays readable afterwards.
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_access_log_tenant_time", "access_log", ["tenant", "at"])
    op.create_index("ix_access_log_key_time", "access_log", ["api_key_id", "at"])

    # f92c4d1a7b60 set ALTER DEFAULT PRIVILEGES for vinea_app, so these two tables
    # arrive already granted. The row policies still have to be declared per table.
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE, or the owner -- which is what the application connects as -- would
        # bypass its own policies and this would be decoration. The lesson of
        # f92c4d1a7b60, repeated because it is the failure that reports success.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(_POLICY.format(table=table))


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index("ix_access_log_key_time", table_name="access_log")
    op.drop_index("ix_access_log_tenant_time", table_name="access_log")
    op.drop_table("access_log")
    op.drop_index("uq_api_keys_prefix", table_name="api_keys")
    op.drop_index("ix_api_keys_tenant", table_name="api_keys")
    op.drop_table("api_keys")
