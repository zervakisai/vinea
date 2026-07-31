"""The three promises, their targets, and what spending the budget means.

Written down before anything measured them, because an error budget chosen after
the first breach is a number chosen to excuse the breach.

The arithmetic, stated plainly so nobody has to be surprised by it later:
**99% over 30 days, one advisory per tenant per day, is 0.3 missed advisories per
tenant per month.** Ten tenants makes three misses across the fleet -- roughly one
bad night per ten days before the budget is gone. That is uncomfortable, and it is
what 99% means. A target that sounds modest and turns out to be strict is the
normal experience of writing one down honestly for the first time.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Objective:
    """One promise, with the target and the window it is judged over.

    `higher_is_better` exists because two of the three are "at least this much"
    and one is "at most this much", and encoding that as a flag beats three
    separate comparison functions that will eventually disagree.
    """

    key: str
    description: str
    target: float
    window_days: int
    higher_is_better: bool
    unit: str
    # Does breaching this warrant interrupting someone? ADR-010 already drew this
    # line in prose -- "the degraded-rate objective never pages, because nothing is
    # broken for a grower when it breaches" -- and prose is not enforceable. It is a
    # field so the notifier can carry the distinction to whatever reads the message,
    # rather than every consumer rediscovering which of the three is urgent.
    #
    # Not "should we send it": a breach that is not urgent is still a breach, and
    # dropping it means the only way to learn the fleet went fully deterministic is
    # to go and look, which is the problem the notifier exists to solve.
    pages: bool = True

    def is_met(self, value: float | None) -> bool | None:
        """None in, None out -- an unmeasured objective is not a met one.

        The distinction matters more here than anywhere else in the system: an
        SLI with no data must never report success, because "no advisories were
        late" and "we could not tell whether any advisories were late" look
        identical on a dashboard and mean opposite things.
        """
        if value is None:
            return None
        return value >= self.target if self.higher_is_better else value <= self.target


AVAILABILITY = Objective(
    key="advisory_availability",
    description="Tenant-days with an advisory available by 06:00 in the grower's local morning",
    target=0.99,
    window_days=30,
    higher_is_better=True,
    unit="fraction",
)

READ_LATENCY = Objective(
    key="read_latency_p95_ms",
    description="p95 latency of GET /advisories/{tenant}/{run_date}",
    # Deliberately the READ path. POST enqueues and returns 202, so its latency
    # is a queue property rather than something a grower waits on -- putting it in
    # an SLO would be measuring the system's convenience, not the user's.
    target=300.0,
    window_days=7,
    higher_is_better=False,
    unit="milliseconds",
)

JUDGEMENT_RATE = Objective(
    key="degraded_rate",
    description="Fraction of advisories produced without a model (the deterministic path)",
    target=0.05,
    window_days=7,
    higher_is_better=False,
    unit="fraction",
    # Nothing is broken for a grower when this breaches -- they got an advisory,
    # computed from the same numbers, without the model's framing. Waking someone
    # for a correct answer is how a rota learns to ignore its pager.
    pages=False,
)

OBJECTIVES = (AVAILABILITY, READ_LATENCY, JUDGEMENT_RATE)


@dataclass(frozen=True, slots=True)
class SLIResult:
    """A measured indicator, or an honest absence of one."""

    objective: Objective
    value: float | None
    sample_size: int

    @property
    def met(self) -> bool | None:
        return self.objective.is_met(self.value)

    @property
    def summary(self) -> str:
        if self.value is None:
            return f"{self.objective.key}: no data ({self.sample_size} rows in window)"
        verdict = "OK" if self.met else "BREACH"
        return (
            f"{self.objective.key}: {self.value:.4g} {self.objective.unit} "
            f"vs target {self.objective.target:.4g} [{verdict}, n={self.sample_size}]"
        )


@dataclass(frozen=True, slots=True)
class ErrorBudget:
    """How much failure the target permits, and how much is left.

    Only meaningful for a ratio objective over a countable population, which is
    why `error_budget` returns None for latency: "p95 under 300 ms" does not
    decompose into a number of allowed bad events, and inventing one would
    produce a percentage that looks like the others and means something else.
    """

    objective: Objective
    allowed_failures: float
    observed_failures: int
    sample_size: int

    @property
    def remaining(self) -> float:
        """Failures still affordable. Negative means the budget is overspent."""
        return self.allowed_failures - self.observed_failures

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    @property
    def policy(self) -> str:
        """What spending it *means*, decided in advance or it means nothing."""
        if self.exhausted:
            return (
                "STOP shipping changes that touch the nightly path until the cause "
                "is understood and the budget recovers."
            )
        return "Budget remaining: ship. The eval gate and the audit gate are the guards."


def error_budget(result: SLIResult) -> ErrorBudget | None:
    """The budget for a ratio objective, or None where the idea does not apply."""
    objective = result.objective
    if objective.unit != "fraction" or result.value is None or result.sample_size == 0:
        return None

    if objective.higher_is_better:
        allowed = (1.0 - objective.target) * result.sample_size
        observed = round((1.0 - result.value) * result.sample_size)
    else:
        allowed = objective.target * result.sample_size
        observed = round(result.value * result.sample_size)

    return ErrorBudget(
        objective=objective,
        allowed_failures=allowed,
        observed_failures=observed,
        sample_size=result.sample_size,
    )
