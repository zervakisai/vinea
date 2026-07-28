"""`python -m vinea.jobs` -- enqueue a night, drain it, or inspect the queue.

A thin operational CLI over the phase 8 pieces, so the queue is drivable by hand for
the verification steps and for a real cron entrypoint. Deployment wires a
scheduler (cron, systemd timer, cloud scheduler) to `enqueue`, and a process
manager to `work`; both are one command here.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlmodel import Session

from vinea.db.session import make_engine
from vinea.jobs import metrics, queue, scheduler, worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vinea.jobs", description="Batch queue operations.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_enqueue = sub.add_parser("enqueue", help="Enqueue one task per active tenant for a run date.")
    p_enqueue.add_argument("--run-date", type=date.fromisoformat, default=date.today())
    p_enqueue.add_argument("--tenant", action="append", help="Enqueue only this tenant (repeatable).")

    p_work = sub.add_parser("work", help="Claim and process tasks until the queue drains.")
    p_work.add_argument("--worker-id", default="worker-1")
    p_work.add_argument("--max-tasks", type=int, default=None)

    sub.add_parser("status", help="Print current queue depth by status.")

    p_requeue = sub.add_parser("requeue", help="Return a failed task to the queue.")
    p_requeue.add_argument("--tenant", required=True)
    p_requeue.add_argument("--run-date", type=date.fromisoformat, required=True)

    args = parser.parse_args(argv)
    engine = make_engine()

    if args.command == "enqueue":
        with Session(engine) as session:
            newly = scheduler.enqueue_nightly(session, run_date=args.run_date, tenants=args.tenant)
        print(f"enqueued {len(newly)} new task(s) for {args.run_date}: {', '.join(newly) or '(none)'}")
        return 0

    if args.command == "work":
        processed = worker.run_worker(
            worker_id=args.worker_id, engine=engine, max_tasks=args.max_tasks
        )
        print(f"{args.worker_id} processed {processed} task(s)")
        return 0

    if args.command == "status":
        with Session(engine) as session:
            depth = metrics.current_depth(session)
        print(
            f"queued={depth['queued']} running={depth['running']} "
            f"done={depth['done']} failed={depth['failed']}"
        )
        return 0

    if args.command == "requeue":
        with Session(engine) as session:
            task = queue.requeue_failed(session, tenant=args.tenant, run_date=args.run_date)
        print("requeued" if task is not None else "no failed task to requeue")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
