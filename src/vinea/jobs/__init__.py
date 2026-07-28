"""Batch execution: the queue, the workers, the scheduler, and the router.

phase 8 is where one advisory becomes a fleet of them running overnight. The pieces:

  degraded.py   build a DailyFarmAdvisory with NO model call, deterministically
  router.py     decide, from the features alone, whether a day even needs the LLM
  worker.py     SELECT ... FOR UPDATE SKIP LOCKED, run the work, retry with one owner
  scheduler.py  enqueue one task per (tenant, run_date), idempotently
  metrics.py    sample queue depth into the DB so it can be charted
  tenancy.py    per-tenant budgets and an exact (never similarity) feature cache

The through-line is the same boundary the core drew: the deterministic core can
produce a complete, honest advisory on its own, so "no model available" and
"this day is too clear-cut to bother a model" both degrade to Python rather than
to failure.
"""
