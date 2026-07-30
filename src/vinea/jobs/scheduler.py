"""The nightly scheduler: one task per (tenant, run_date), idempotently.

The advisory runs overnight, once per grower. The scheduler's whole
job is to turn "it's tonight, and these tenants exist" into one `advisory_tasks`
row each -- and to be safe to run more than once, because schedulers get retried,
double-fired by overlapping cron windows, and run manually alongside the
automated trigger.

Idempotency is the (tenant, run_date) key doing its work: `enqueue` is ON CONFLICT
DO NOTHING, so N invocations of the scheduler for the same night produce exactly
ONE task per tenant, total, no matter how many times it runs. That's the property
that lets a nightly cron and a nervous operator both press the button without
creating duplicate advisories.

This module deliberately does not depend on APScheduler or any scheduling daemon.
The trigger -- cron, a systemd timer, a cloud scheduler -- is deployment's
concern; `enqueue_nightly` is the idempotent body it calls, and keeping it a plain
function is what makes it testable without a clock.
"""

from __future__ import annotations

from datetime import date

from sqlmodel import Session, select

from vinea.db.models import AdvisoryTask, GrowerConfig
from vinea.jobs import queue


def active_tenants(session: Session) -> list[str]:
    """Every tenant with a current grower_config -- the ones to advise tonight.

    A tenant is "active" if it has an open config row. This is the multi-tenancy
    seam: tenancy is a data fact (who has config), not a code list, so
    onboarding a grower is an INSERT into grower_config and nothing here changes.
    """
    rows = session.exec(
        select(GrowerConfig.tenant).where(GrowerConfig.valid_to.is_(None)).distinct()
    ).all()
    return sorted(rows)


def enqueue_nightly(
    session: Session,
    *,
    run_date: date,
    tenants: list[str] | None = None,
) -> list[str]:
    """Enqueue one task per active tenant for `run_date`. Idempotent. Commits.

    Returns the tenants for which a *new* task was created (existing ones are left
    untouched by `enqueue`'s DO NOTHING), so a caller can log "enqueued 12 of 40
    tonight; the other 28 were already queued from the earlier run."

    `tenants` overrides the active-tenant lookup for tests and targeted backfills;
    production passes None and advises everyone with config.
    """
    targets = tenants if tenants is not None else active_tenants(session)

    newly_enqueued: list[str] = []
    for tenant in targets:
        before = _task_exists(session, tenant=tenant, run_date=run_date)
        queue.enqueue(session, tenant=tenant, run_date=run_date)
        if not before:
            newly_enqueued.append(tenant)

    session.commit()
    return newly_enqueued


def _task_exists(session: Session, *, tenant: str, run_date: date) -> bool:
    return (
        session.exec(
            select(AdvisoryTask.id).where(
                AdvisoryTask.tenant == tenant, AdvisoryTask.run_date == run_date
            )
        ).first()
        is not None
    )
