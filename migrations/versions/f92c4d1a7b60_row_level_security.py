"""row-level security on every tenant-scoped table

Revision ID: f92c4d1a7b60
Revises: e1b7c904ad35
Create Date: 2026-07-29 17:22:41.556203

The fourth time this project trades a promise for a guarantee, and the biggest.
Tenant isolation is currently 29 `WHERE tenant = :tenant` clauses across five
modules; one forgotten filter serves another grower's data with a correct-looking
200 and no log line. After this migration a query with no tenant filter returns
*nothing* instead of *everything*.

Three details that decide whether this works or merely looks like it does:

**FORCE, not just ENABLE.** A table's owner bypasses its own row policies. The
application connects as `vinea`, which owns every table, so `ENABLE ROW LEVEL
SECURITY` alone would be decoration. `FORCE` subjects the owner too.

**And FORCE is still not enough, which is this migration's real lesson.** The
first version of this file did exactly the above, applied cleanly, and was
*completely inert*: `vinea` is the container's bootstrap role and is
`rolsuper = true, rolbypassrls = true`. Superusers bypass row security
unconditionally -- FORCE does not reach them. Every policy was correct, every
table reported `rowsecurity = true`, and a scoped session still read every
tenant's rows. A security control that reports success while doing nothing is
worse than one that is absent, because it stops anyone looking.

So this migration also creates `vinea_app`: NOSUPERUSER, NOBYPASSRLS, NOLOGIN.
Nothing connects as it -- the application `SET LOCAL ROLE`s into it for the
duration of a transaction, which keeps one DATABASE_URL and one connection pool
while making the queries run under a role the policies actually apply to.

**`current_setting(..., true)` — the second argument is the whole safety
property.** Without it, an unset variable raises; with it, the setting comes back
NULL, `tenant = NULL` is NULL, and the row is filtered out. So a connection that
never declared a tenant sees nothing. Fail closed, by arithmetic rather than by
an explicit check somebody has to remember to write.

**An ops escape, and its limits stated.** The worker claims tasks across all
tenants (`SKIP LOCKED` over the whole queue) and `/ops/*` aggregates across them
by design. Both set `vinea.ops = 'on'` for their transaction. That means this
defends against *forgetting* a filter -- the threat that actually happens -- and
not against application code deliberately opting out. The stronger version is a
separate database role without the escape, which costs a second DATABASE_URL in
every deployment; ADR-009 records it as the revisit trigger rather than pretending
the weaker version is the strong one.

Migrations themselves are unaffected: DDL is not subject to row policies, and
`alembic upgrade` runs as the owner.
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f92c4d1a7b60"
down_revision: Union[str, Sequence[str], None] = "e1b7c904ad35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every table with a `tenant` column. `advisory_citations` and `eval_runs` are
# NOT here and that is deliberate, not an oversight: neither carries a tenant, and
# both reach one only through `advisories.id`. A policy over a join is a policy
# whose cost surprises people, and the foreign key with ON DELETE CASCADE already
# ties their lifetime to a row that IS protected. Called out in ADR-009 as a
# known gap: a citation row leaks a locator, not a grower's advice.
TENANT_TABLES = (
    "weather_observations",
    "grower_config",
    "advisories",
    "feature_cache",
    "advisory_tasks",
)

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


APP_ROLE = "vinea_app"


def upgrade() -> None:
    """Create the restricted role, then enable, force and police each table.

    `WITH CHECK` mirrors `USING` so the policy governs writes as well as reads.
    Without it a session scoped to tenant A could INSERT a row belonging to
    tenant B -- it would then be unable to read it back, which is a wonderfully
    confusing bug to debug and a real one to leave available.
    """
    # Idempotent: migrations are re-run against databases at unknown states, and
    # roles are cluster-wide so another database on the same server may have
    # created it already.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                -- NOLOGIN on purpose: this role is never connected to, only
                -- assumed with SET LOCAL ROLE. A role that cannot log in cannot
                -- have its password leaked, because it does not have one.
                CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
            END IF;
        END
        $$;
        """
    )
    # The owner must be a member of the role to SET ROLE into it.
    op.execute(f"GRANT {APP_ROLE} TO CURRENT_USER")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    # Serial primary keys need the sequence, or every INSERT fails with a
    # permission error that reads nothing like "you forgot a grant".
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    # Tables created by LATER migrations would otherwise arrive ungranted, and
    # the failure would land on whoever adds phase 18's first table.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(_POLICY.format(table=table))


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    # The role is left in place. It is cluster-wide and may be granted objects in
    # other databases; dropping it here would reach outside what this migration
    # created, the same reasoning that leaves the `vector` extension alone in
    # c73a51e8d4b2.
