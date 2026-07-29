"""phase 8 (S3.4) -- queue-depth metrics, sampled into the DB so a chart can read them."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import open_ops_session
from vinea.jobs import metrics, queue

pytestmark = pytest.mark.db

RUN_DATE = date(2025, 2, 8)


def test_current_depth_counts_by_status(committing_db):
    with open_ops_session(committing_db) as s:
        queue.enqueue(s, tenant="a", run_date=RUN_DATE)
        queue.enqueue(s, tenant="b", run_date=RUN_DATE)
        s.commit()
    with open_ops_session(committing_db) as s:
        queue.claim_one(s, worker_id="w1")  # one -> running
    with open_ops_session(committing_db) as s:
        depth = metrics.current_depth(s)
    assert depth["queued"] == 1
    assert depth["running"] == 1


def test_sample_writes_a_point_the_chart_can_read(committing_db):
    with open_ops_session(committing_db) as s:
        queue.enqueue(s, tenant="a", run_date=RUN_DATE)
        queue.enqueue(s, tenant="b", run_date=RUN_DATE)
        s.commit()
    with open_ops_session(committing_db) as s:
        sample = metrics.sample_queue_depth(s)
        s.commit()
        assert sample.queued == 2

    with open_ops_session(committing_db) as s:
        series = metrics.depth_over_time(s)
    assert len(series) == 1
    assert series[0].queued == 2
