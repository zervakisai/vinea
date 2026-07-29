"""phase 8 -- the queue, SKIP LOCKED, idempotency, and one-owner retry.

These use `committing_db` (real commits, TRUNCATE isolation) rather than the
rollback fixture, because SKIP LOCKED and idempotent enqueue are claims about what
concurrent *committed* transactions do -- unobservable inside one transaction that
never lands.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func
from sqlmodel import Session, select

from vinea.db.models import AdvisoryTask
from vinea.jobs import queue

pytestmark = pytest.mark.db

RUN_DATE = date(2025, 2, 8)


def _session(engine) -> Session:
    """A committing session scoped to ops (phase 17).

    The worker and the queue are cross-tenant by design -- one queue, SKIP
    LOCKED, every tenant -- so the tests declare the same scope the worker
    declares. A bare `Session(engine)` would now see nothing.
    """
    from vinea.db.session import scope_to_ops

    session = Session(engine)
    scope_to_ops(session)
    return session


# --- S3.2: idempotent enqueue -----------------------------------------------


def test_enqueue_is_idempotent_on_tenant_run_date(committing_db):
    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        s.commit()

    with _session(committing_db) as s:
        tasks = s.exec(select(AdvisoryTask).where(AdvisoryTask.tenant == "acme")).all()
    assert len(tasks) == 1, "a second enqueue for the same night must not duplicate"


def test_enqueue_does_not_reset_an_in_flight_task(committing_db):
    # DO NOTHING, not DO UPDATE: a running task re-enqueued stays running.
    with _session(committing_db) as s:
        task = queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        task.status = "running"
        s.add(task)
        s.commit()

    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        s.commit()

    with _session(committing_db) as s:
        task = s.exec(select(AdvisoryTask).where(AdvisoryTask.tenant == "acme")).one()
    assert task.status == "running", "re-enqueue must not reset an in-flight task to queued"


# --- S3.1: SKIP LOCKED ------------------------------------------------------


def test_claim_marks_running_and_increments_attempts(committing_db):
    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        s.commit()

    with _session(committing_db) as s:
        claimed = queue.claim_one(s, worker_id="w1")
        assert claimed is not None
        assert claimed.status == "running"
        assert claimed.locked_by == "w1"
        assert claimed.attempts == 1


def test_two_concurrent_claims_never_take_the_same_task(committing_db):
    """The heart of S3.1: two workers on two connections, one task. One gets it,
    the other must SKIP it and get nothing -- not block, not double-claim."""
    with _session(committing_db) as s:
        queue.enqueue(s, tenant="only", run_date=RUN_DATE)
        s.commit()

    s1, s2 = _session(committing_db), _session(committing_db)
    try:
        # s1 opens the claim transaction and locks the row, but does NOT commit --
        # the lock is held.
        row1 = s1.execute(
            select(AdvisoryTask)
            .where(AdvisoryTask.status == "queued", AdvisoryTask.run_after <= func.now())
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        assert row1 is not None, "s1 should have claimed the only task"

        # While s1 holds the lock, s2 tries the same claim. SKIP LOCKED means it
        # steps over the locked row and finds nothing -- it does not block, and it
        # does not get the same row.
        row2 = s2.execute(
            select(AdvisoryTask)
            .where(AdvisoryTask.status == "queued", AdvisoryTask.run_after <= func.now())
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        assert row2 is None, "SKIP LOCKED must make the second worker skip the locked row"
    finally:
        s1.rollback()
        s2.rollback()
        s1.close()
        s2.close()


def test_claim_returns_none_on_an_empty_queue(committing_db):
    with _session(committing_db) as s:
        assert queue.claim_one(s, worker_id="w1") is None


def test_a_task_with_a_future_run_after_is_not_yet_claimable(committing_db):
    # Backoff enforcement: run_after in the future -> not eligible.
    with _session(committing_db) as s:
        task = queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        task.run_after = s.execute(select(func.now() + timedelta(hours=1))).scalar_one()
        s.add(task)
        s.commit()

    with _session(committing_db) as s:
        assert queue.claim_one(s, worker_id="w1") is None


# --- S3.3: one-owner retry, backoff, and the deadline -----------------------


def test_failure_below_max_attempts_reenqueues_with_backoff(committing_db):
    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE, max_attempts=3)
        s.commit()
    with _session(committing_db) as s:
        task = queue.claim_one(s, worker_id="w1")  # attempts -> 1
        decision = queue.mark_failed_or_retry(s, task, error="boom")

    assert decision.startswith("retry_in_")
    with _session(committing_db) as s:
        task = s.exec(select(AdvisoryTask).where(AdvisoryTask.tenant == "acme")).one()
        assert task.status == "queued"
        assert task.run_after > task.created_at  # pushed into the future


def test_failure_at_max_attempts_fails_permanently(committing_db):
    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE, max_attempts=1)
        s.commit()
    with _session(committing_db) as s:
        task = queue.claim_one(s, worker_id="w1")  # attempts -> 1, == max
        decision = queue.mark_failed_or_retry(s, task, error="boom")

    assert decision == "exhausted"
    with _session(committing_db) as s:
        task = s.exec(select(AdvisoryTask).where(AdvisoryTask.tenant == "acme")).one()
        assert task.status == "failed"


def test_a_passed_deadline_fails_even_with_attempts_remaining(committing_db):
    # The deadline is never extended: a task stuck across attempts dies rather than
    # retrying into the next night, even though attempts remain.
    with _session(committing_db) as s:
        task = queue.enqueue(s, tenant="acme", run_date=RUN_DATE, max_attempts=5)
        task.deadline_at = s.execute(select(func.now() - timedelta(minutes=1))).scalar_one()
        s.add(task)
        s.commit()
    with _session(committing_db) as s:
        task = queue.claim_one(s, worker_id="w1")  # attempts -> 1, well under 5
        decision = queue.mark_failed_or_retry(s, task, error="slow")

    assert decision == "deadline"
    with _session(committing_db) as s:
        task = s.exec(select(AdvisoryTask).where(AdvisoryTask.tenant == "acme")).one()
        assert task.status == "failed"


def test_requeue_failed_revives_only_failed_tasks(committing_db):
    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE, max_attempts=1)
        s.commit()
    with _session(committing_db) as s:
        task = queue.claim_one(s, worker_id="w1")
        queue.mark_failed_or_retry(s, task, error="boom")

    with _session(committing_db) as s:
        revived = queue.requeue_failed(s, tenant="acme", run_date=RUN_DATE)
        assert revived is not None
        assert revived.status == "queued"
        assert revived.attempts == 0

    with _session(committing_db) as s:
        assert queue.requeue_failed(s, tenant="nobody", run_date=RUN_DATE) is None


def test_reap_returns_a_dead_workers_task_to_the_queue(committing_db):
    with _session(committing_db) as s:
        queue.enqueue(s, tenant="acme", run_date=RUN_DATE)
        s.commit()
    with _session(committing_db) as s:
        task = queue.claim_one(s, worker_id="w1")
        # Simulate a worker that died mid-run: running, but locked long ago.
        task.locked_at = s.execute(select(func.now() - timedelta(hours=1))).scalar_one()
        s.add(task)
        s.commit()

    with _session(committing_db) as s:
        reaped = queue.reap_expired_leases(s, older_than_seconds=60)
    assert reaped == 1
    with _session(committing_db) as s:
        task = s.exec(select(AdvisoryTask).where(AdvisoryTask.tenant == "acme")).one()
        assert task.status == "queued"
