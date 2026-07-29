"""Service level objectives, measured in SQL over rows that already exist.

Three objectives, and each is a question about the tables this system has been
filling since phase 6:

  **Advisory availability** -- did an advisory exist by 06:00 in the grower's own
  morning? `advisories.created_at` and `grower_config.timezone`.
  **Read latency** -- p95 of the grower-facing GET. The one SLI with no row
  behind it, which is why it is measured differently and why that difference is
  called out rather than smoothed over.
  **Judgement rate** -- what fraction of advisories came from the deterministic
  path. `advisories.degraded`, a column since phase 6.

Why SQL and not a metrics pipeline: a counter can only measure what somebody
remembered to increment, and every one of these questions is already answerable
from data the system persists because ADR-001 said it could not be recomputed.
A Prometheus histogram would serve them *continuously*, which is a serving
problem, not a definition problem -- and defining them in SQL means the
definition is checkable by anyone with psql and no agreement about label
cardinality.

What this package deliberately does not contain: a scrape endpoint nothing
scrapes, and an alerting daemon. ADR-010 explains both.
"""

from vinea.slo.objectives import (
    OBJECTIVES,
    ErrorBudget,
    Objective,
    SLIResult,
    error_budget,
)
from vinea.slo.queries import (
    availability_by_day,
    degraded_rate,
    measure_all,
)

__all__ = [
    "OBJECTIVES",
    "ErrorBudget",
    "Objective",
    "SLIResult",
    "availability_by_day",
    "degraded_rate",
    "error_budget",
    "measure_all",
]
