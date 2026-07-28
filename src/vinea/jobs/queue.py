"""The queue operations: enqueue, claim (SKIP LOCKED), complete, retry.

This module is to `advisory_tasks` what `repository.py` is to `advisories` -- the
only place the rest of the system touches the table. It holds the two pieces of
real concurrency logic in phase 8: the SKIP LOCKED claim, and the one-owner retry
decision.

Transactions: unlike `repository.py`, a few functions here commit, because a
claim's whole purpose is to be visible to *other* workers immediately. Where that
matters it's called out. `enqueue` follows the repository convention and leaves
the commit to the caller, so the scheduler can enqueue a whole night atomically.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from vinea.db.models import AdvisoryTask

# Retry backoff: attempt N waits BASE * 2**(N-1), capped. Data on the row, so a
# crash mid-backoff doesn't lose the schedule.
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600

# How long a whole day's advisory may take across all its worker attempts, from
# first enqueue. ONE deadline, never extended (S3.3).
DEFAULT_DEADLINE_SECONDS = 1800


def enqueue(
    session: Session,
    *,
    tenant: str,
    run_date: date,
    max_attempts: int = 3,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
) -> AdvisoryTask:
    """Enqueue one (tenant, run_date) task, idempotently.

    ON CONFLICT DO NOTHING on the natural key: firing the scheduler twice, or a
    manual re-run alongside the nightly, does not create a second task or disturb
    one that's already running. This is the queue-side twin of the advisory
    UPSERT -- same key, so the whole night is re-runnable.

    DO NOTHING, not DO UPDATE, and that difference is deliberate: a task that's
    mid-flight or already failed should not be silently reset to 'queued' by a
    re-enqueue. Requeuing a failed day is an explicit action (`requeue_failed`),
    not a side effect of the scheduler running again.

    Does not commit -- the scheduler enqueues a whole night in one transaction.
    """
    deadline = session.execute(
        select(func.now() + timedelta(seconds=deadline_seconds))
    ).scalar_one()

    statement = (
        pg_insert(AdvisoryTask)
        .values(
            tenant=tenant,
            run_date=run_date,
            status="queued",
            max_attempts=max_attempts,
            deadline_at=deadline,
        )
        .on_conflict_do_nothing(constraint="uq_advisory_tasks_idempotency")
        .returning(AdvisoryTask.id)
    )
    inserted_id = session.execute(statement).scalar_one_or_none()
    session.flush()

    if inserted_id is not None:
        return session.get(AdvisoryTask, inserted_id)
    # Already existed: return the incumbent unchanged.
    return session.exec(
        select(AdvisoryTask).where(
            AdvisoryTask.tenant == tenant, AdvisoryTask.run_date == run_date
        )
    ).one()


def claim_one(session: Session, *, worker_id: str) -> AdvisoryTask | None:
    """Claim the next runnable task, or return None. Commits.

    This is the SKIP LOCKED heart of S3.1. The query:

        SELECT ... FROM advisory_tasks
        WHERE status = 'queued' AND run_after <= now()
        ORDER BY run_after, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1

    `FOR UPDATE` takes a row lock; `SKIP LOCKED` makes a concurrent worker step
    over any row this one already holds rather than blocking on it. So N workers
    hammering this query each pull a *different* task with no coordination, no
    external broker, and no lost or double-dispatched work. That's the whole
    lesson of ADR-003 in one query.

    The claim commits before returning: flipping status to 'running' has to be
    visible to the other workers immediately, or two of them could re-claim the
    same row the instant the lock is released. The lock guarantees exclusivity
    *while held*; the committed status change is what makes it stick after.

    `run_after <= now()` is what enforces backoff -- a re-enqueued task with a
    future `run_after` is simply not eligible yet, and the same predicate the
    partial index is built on keeps the claim cheap.
    """
    row = session.execute(
        select(AdvisoryTask)
        .where(AdvisoryTask.status == "queued", AdvisoryTask.run_after <= func.now())
        .order_by(AdvisoryTask.run_after, AdvisoryTask.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalar_one_or_none()

    if row is None:
        return None

    now = session.execute(select(func.clock_timestamp())).scalar_one()
    row.status = "running"
    row.locked_by = worker_id
    row.locked_at = now
    row.attempts += 1
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def mark_done(session: Session, task: AdvisoryTask, *, advisory_id: int) -> None:
    """Mark a claimed task finished and link the advisory it produced.

    Does not commit: the worker marks done in the SAME transaction that saves the
    advisory, so the advisory and its task-completion land together or not at
    all. A committed advisory with a task still 'running' would be re-processed; a
    'done' task with no advisory would be a lie.
    """
    task.status = "done"
    task.advisory_id = advisory_id
    task.locked_by = None
    task.last_error = None
    session.add(task)


def mark_failed_or_retry(session: Session, task: AdvisoryTask, *, error: str) -> str:
    """One owner of retry. Decide give-up vs backoff-re-enqueue. Commits.

    This is the S3.3 footgun made into a single function so there is exactly ONE
    place that decides a *worker-level* retry. pydantic-ai's Agent owns its own
    network/validation retries (ModelRetry, with jitter); this layer must never
    wrap those in a second retry loop, or a single bad day fans out to attempts x
    SDK-retries calls and the first symptom is the bill.

    The decision, in order:
      1. If the day's single deadline has passed -> fail permanently. A deadline
         is never extended, so a task stuck across attempts dies rather than
         retrying into the next night.
      2. Else if attempts >= max_attempts -> fail permanently.
      3. Else -> back to 'queued' with `run_after` pushed out by exponential
         backoff. The row already counted this attempt at claim time.

    Returns the decision as a short string, for logging/metrics.
    """
    now = session.execute(select(func.clock_timestamp())).scalar_one()
    task.last_error = error[:2000]

    deadline_passed = task.deadline_at is not None and now >= task.deadline_at
    exhausted = task.attempts >= task.max_attempts

    if deadline_passed or exhausted:
        task.status = "failed"
        task.locked_by = None
        decision = "deadline" if deadline_passed else "exhausted"
    else:
        backoff = min(BACKOFF_BASE_SECONDS * (2 ** (task.attempts - 1)), BACKOFF_CAP_SECONDS)
        task.status = "queued"
        task.locked_by = None
        task.run_after = session.execute(
            select(func.now() + timedelta(seconds=backoff))
        ).scalar_one()
        decision = f"retry_in_{backoff}s"

    session.add(task)
    session.commit()
    return decision


def requeue_failed(session: Session, *, tenant: str, run_date: date) -> AdvisoryTask | None:
    """Explicitly return a failed task to the queue, attempts reset. Commits.

    Separate from `enqueue` on purpose: reviving a failed day is a human decision
    (the underlying problem was fixed), not something the scheduler should do by
    re-running. The deadline is refreshed here, because a new attempt at a fixed
    problem deserves a fresh clock.
    """
    task = session.exec(
        select(AdvisoryTask).where(
            AdvisoryTask.tenant == tenant, AdvisoryTask.run_date == run_date
        )
    ).one_or_none()
    if task is None or task.status != "failed":
        return None

    now = session.execute(select(func.now())).scalar_one()
    task.status = "queued"
    task.attempts = 0
    task.run_after = now
    task.deadline_at = now + timedelta(seconds=DEFAULT_DEADLINE_SECONDS)
    task.locked_by = None
    task.last_error = None
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def reap_expired_leases(session: Session, *, older_than_seconds: int = 900) -> int:
    """Return tasks whose worker died mid-run to the queue. Commits.

    A worker that crashes leaves its task 'running' forever. Because all state is
    on the row (ADR-003), recovery needs no coordination: any process can find
    rows 'running' with a `locked_at` older than a lease window and flip them back
    to 'queued'. Returns how many were reaped.

    The deadline still applies -- a reaped task past its deadline will fail on its
    next attempt rather than retrying forever.
    """
    cutoff = session.execute(
        select(func.now() - timedelta(seconds=older_than_seconds))
    ).scalar_one()
    stuck = session.exec(
        select(AdvisoryTask).where(
            AdvisoryTask.status == "running", AdvisoryTask.locked_at < cutoff
        )
    ).all()
    for task in stuck:
        task.status = "queued"
        task.locked_by = None
        task.last_error = "lease expired; worker presumed dead"
        session.add(task)
    session.commit()
    return len(stuck)
