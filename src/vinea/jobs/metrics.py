"""Queue-depth metrics, sampled into the DB so they can be charted.

The autoscaling argument -- scale the worker fleet on queue depth, not
CPU, because the work is I/O-bound on the model API -- only becomes actionable if
queue depth is visible over time. So a worker (or a cron) samples the counts into
`queue_depth_samples`, and S6's operator dashboard draws the line. This module is
deliberately tiny: the point isn't the metric, it's that the metric lives where a
chart can reach it.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlmodel import Session, select

from vinea.db.models import AdvisoryTask, QueueDepthSample


def current_depth(session: Session) -> dict[str, int]:
    """Count tasks by status right now. One grouped query, not four COUNTs."""
    rows = session.execute(
        select(AdvisoryTask.status, func.count()).group_by(AdvisoryTask.status)
    ).all()
    counts = {"queued": 0, "running": 0, "done": 0, "failed": 0}
    for status, n in rows:
        counts[status] = n
    return counts


def sample_queue_depth(session: Session) -> QueueDepthSample:
    """Write one queue-depth snapshot. Does not commit (caller owns the txn).

    Called on a timer by the worker loop or a small cron. Each row is a point on
    the chart S6 draws; the sampling cadence is the chart's resolution.
    """
    counts = current_depth(session)
    sample = QueueDepthSample(
        queued=counts["queued"],
        running=counts["running"],
        done=counts["done"],
        failed=counts["failed"],
    )
    session.add(sample)
    session.flush()
    return sample


def depth_over_time(session: Session, *, limit: int = 500) -> list[QueueDepthSample]:
    """The samples, newest first -- what S6's queue-depth chart reads."""
    return list(
        session.exec(
            select(QueueDepthSample).order_by(QueueDepthSample.sampled_at.desc()).limit(limit)
        ).all()
    )
