"""The only module the rest of the system calls to reach the database.

Everything above this line works in contracts -- `DailyFarmAdvisory`, `Deps`,
`WeatherRow`. Everything below it works in rows. Keeping that boundary in one
file is what lets S5's API rule ("the API reads the DB, it does not compute") be
a one-line import rather than a convention nobody enforces.

Transactions belong to the caller. Nothing here commits: a worker that writes an
advisory and marks its task done (S3) needs both to land or neither, and a
repository that commits on its own takes that choice away.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from vinea.contracts import DailyFarmAdvisory
from vinea.db.mapping import (
    advisory_to_row,
    deps_hash,
    deps_to_row,
    row_to_advisory,
    row_to_deps,
)
from vinea.db.models import Advisory, GrowerConfig
from vinea.deps import Deps

# ---------------------------------------------------------------------------
# advisories
# ---------------------------------------------------------------------------


def save_advisory(
    session: Session,
    advisory: DailyFarmAdvisory,
    *,
    tenant: str,
    run_date: date,
    deps: Deps,
    model_id: str | None = None,
    prompt_name: str | None = None,
    prompt_version: str | None = None,
    prompt_source: str | None = None,
    code_sha: str | None = None,
    dataset_version: str | None = None,
    trace_id: str | None = None,
    degraded: bool = False,
    pre_correction_output: dict | None = None,
) -> Advisory:
    """UPSERT one advisory on its (tenant, run_date) idempotency key.

    Re-running a night overwrites that night's advisory instead of appending a
    second one. This is the property S3's queue is built on: a task can be
    retried, redelivered, or run twice by two workers racing, and the grower
    still ends up with exactly one advisory for the date. Getting that guarantee
    from a unique index costs nothing and cannot be forgotten; getting it from
    "the worker checks first" is a race.

    Note the *shape* of the guarantee -- last writer wins, not first. A rerun is
    normally a correction (better data, a fixed prompt), so the newer answer is
    the one worth keeping.
    """
    row = advisory_to_row(
        advisory,
        tenant=tenant,
        run_date=run_date,
        deps=deps,
        model_id=model_id,
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        prompt_source=prompt_source,
        code_sha=code_sha,
        dataset_version=dataset_version,
        trace_id=trace_id,
        degraded=degraded,
        pre_correction_output=pre_correction_output,
    )

    # `id` is autoincrement and `created_at` has a server default: sending either
    # as NULL would override the database's own answer.
    values = row.model_dump(exclude={"id", "created_at"})
    # Never rewrite the key we matched on.
    updatable = {k: v for k, v in values.items() if k not in ("tenant", "run_date")}

    statement = (
        pg_insert(Advisory)
        .values(**values)
        .on_conflict_do_update(constraint="uq_advisories_idempotency", set_=updatable)
        .returning(Advisory.id)
    )
    advisory_id = session.execute(statement).scalar_one()
    session.flush()
    # Discard any stale identity-map copy from an earlier read in this session:
    # the UPSERT went through Core, so the ORM hasn't seen it.
    session.expire_all()
    return session.get(Advisory, advisory_id)


def get_advisory(session: Session, *, tenant: str, run_date: date) -> DailyFarmAdvisory | None:
    """The contract for one night, or None. Revalidated on the way out."""
    row = session.exec(
        select(Advisory).where(Advisory.tenant == tenant, Advisory.run_date == run_date)
    ).one_or_none()
    return row_to_advisory(row) if row is not None else None


def get_advisory_row(session: Session, *, tenant: str, run_date: date) -> Advisory | None:
    """The raw row, for callers that need provenance rather than the advice.

    Separate from `get_advisory` on purpose: `trace_id`, `degraded` and the
    prompt tags are *about* the advisory, not part of it, and the contract should
    not grow fields because a storage layer wanted them.
    """
    return session.exec(
        select(Advisory).where(Advisory.tenant == tenant, Advisory.run_date == run_date)
    ).one_or_none()


def list_advisory_rows(
    session: Session, *, tenant: str, start: date | None = None, end: date | None = None
) -> list[Advisory]:
    """One tenant's advisories over a date window, newest first (S5.2's GET)."""
    statement = select(Advisory).where(Advisory.tenant == tenant)
    if start is not None:
        statement = statement.where(Advisory.run_date >= start)
    if end is not None:
        statement = statement.where(Advisory.run_date <= end)
    return list(session.exec(statement.order_by(Advisory.run_date.desc())).all())


def list_all_advisory_rows(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 500,
) -> list[Advisory]:
    """Advisories across ALL tenants, newest first -- the operator's cross-tenant view.

    Separate from `list_advisory_rows` because it deliberately has no tenant
    filter: the operator quality monitor (S6.4) aggregates degraded rate and
    confidence over everyone. This is ops surface, so the API gates it behind the
    ops key, not a tenant key -- the same tenant-vs-operator credential split as
    `/ops/queue` (S5.3).
    """
    statement = select(Advisory)
    if start is not None:
        statement = statement.where(Advisory.run_date >= start)
    if end is not None:
        statement = statement.where(Advisory.run_date <= end)
    statement = statement.order_by(Advisory.run_date.desc(), Advisory.id.desc()).limit(limit)
    return list(session.exec(statement).all())


# ---------------------------------------------------------------------------
# grower_config -- deps as rows
# ---------------------------------------------------------------------------


def save_grower_config(
    session: Session, deps: Deps, *, tenant: str, location: str, region: str
) -> GrowerConfig:
    """Open a new config version for a block.

    Versioned, not overwritten: `valid_to` on the previous row is closed and a
    new row opens. An advisory from March has to stay explicable against March's
    thresholds, and `advisories.deps_hash` is the pointer that makes that work --
    which only means anything if the old row still exists.
    """
    current = _current_config_row(session, tenant=tenant, location=location)
    row = deps_to_row(deps, tenant=tenant, location=location, region=region)

    if current is not None and current.deps_hash == row.deps_hash:
        # Identical thresholds: opening a second version would be noise in the
        # audit trail, not information.
        return current

    # ONE clock read, used for both sides of the changeover, so the old version
    # closes at exactly the instant the new one opens -- no gap in which no
    # config was in force, no overlap in which two were.
    #
    # clock_timestamp(), NOT now(): in Postgres `now()` is the *transaction*
    # start time, so two versions written inside one transaction would share a
    # valid_from and collide on uq_grower_config_version. clock_timestamp() is
    # the actual wall clock, and it advances between statements.
    changeover = session.execute(select(func.clock_timestamp())).scalar_one()

    if current is not None:
        # Close the old version BEFORE opening the new one: uq_grower_config_open
        # permits only one row per block with valid_to IS NULL, so the reverse
        # order would violate it.
        current.valid_to = changeover
        session.add(current)
        session.flush()

    row.valid_from = changeover
    session.add(row)
    session.flush()
    return row


def _current_config_row(session: Session, *, tenant: str, location: str) -> GrowerConfig | None:
    return session.exec(
        select(GrowerConfig)
        .where(
            GrowerConfig.tenant == tenant,
            GrowerConfig.location == location,
            GrowerConfig.valid_to.is_(None),
        )
        .order_by(GrowerConfig.valid_from.desc())
    ).first()


def get_current_deps(session: Session, *, tenant: str, location: str) -> Deps | None:
    """The Deps in force for a block right now, or None if unconfigured.

    This is the function that makes "a new crop is a config change, not a code
    change" true rather than aspirational: S3's worker calls it instead of
    importing WINE_GRAPES, and adding an olive grove becomes an INSERT.
    """
    row = _current_config_row(session, tenant=tenant, location=location)
    return row_to_deps(row) if row is not None else None


def get_deps_by_hash(session: Session, *, tenant: str, deps_fingerprint: str) -> Deps | None:
    """The Deps a past advisory ran under, found by its stored hash.

    The read that makes `advisories.deps_hash` worth storing: "what did we think
    a vineyard's TAW was when we told them not to irrigate in March?"
    """
    row = session.exec(
        select(GrowerConfig).where(
            GrowerConfig.tenant == tenant, GrowerConfig.deps_hash == deps_fingerprint
        )
    ).first()
    return row_to_deps(row) if row is not None else None


__all__ = [
    "deps_hash",
    "get_advisory",
    "get_advisory_row",
    "get_current_deps",
    "get_deps_by_hash",
    "list_advisory_rows",
    "save_advisory",
    "save_grower_config",
]
