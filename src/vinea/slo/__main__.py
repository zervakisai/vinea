"""`python -m vinea.slo` -- report the objectives, or check them and exit non-zero.

    python -m vinea.slo report          # the table, for a human
    python -m vinea.slo check           # exit 1 if any objective is unmet
    python -m vinea.slo check --strict  # ...or if any cannot be measured

`check` is the SLO equivalent of `alembic check`: a command that answers one
question with an exit code, usable by a person, by cron, or by CI. It is
deliberately **not** alerting -- nothing here notifies anyone, and ADR-010 declined
a notification path until somebody is on a rota. What it adds is a row in
`slo_breaches` per unmet objective, so "how long have we been in breach" becomes
answerable; a live query can only say whether we are.

`--strict` exists because an objective that cannot be measured is not the same as
one that is met, and the two need different exit codes for different callers. A
cron job wants to know about breaches; a release gate wants to know that the
measurement is working at all.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlmodel import Session

from vinea.db.session import make_engine, scope_to_ops
from vinea.slo.objectives import error_budget
from vinea.slo.queries import measure_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vinea.slo", description="Service level objectives.")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("report", "Print every objective and its error budget."),
        ("check", "Exit 1 if any objective is unmet. Records breaches."),
    ):
        parser_for = sub.add_parser(name, help=help_text)
        parser_for.add_argument("--today", type=date.fromisoformat, default=None)
        if name == "check":
            parser_for.add_argument(
                "--strict",
                action="store_true",
                help="Also fail when an objective cannot be measured at all.",
            )
            parser_for.add_argument(
                "--no-record", action="store_true", help="Report breaches without writing rows."
            )

    args = parser.parse_args(argv)
    today = args.today or date.today()

    with Session(make_engine()) as session:
        # Every SLI is fleet-wide, so the cross-tenant scope is declared once here
        # rather than being a property of the queries.
        scope_to_ops(session)
        results = measure_all(session, today=today)

        breaches = [r for r in results if r.met is False]
        unmeasured = [r for r in results if r.met is None]

        for result in results:
            print(result.summary)
            budget = error_budget(result)
            if budget is not None:
                print(
                    f"    budget: {budget.observed_failures} of "
                    f"{budget.allowed_failures:.1f} allowed, "
                    f"{'EXHAUSTED' if budget.exhausted else f'{budget.remaining:.1f} left'}"
                )

        if args.command == "report":
            return 0

        if breaches and not args.no_record:
            from vinea.db.models import SLOBreach

            for result in breaches:
                budget = error_budget(result)
                session.add(
                    SLOBreach(
                        objective=result.objective.key,
                        value=result.value,
                        target=result.objective.target,
                        sample_size=result.sample_size,
                        budget_exhausted=budget.exhausted if budget else None,
                    )
                )
            session.commit()
            print(f"\nrecorded {len(breaches)} breach(es) in slo_breaches")

    if breaches:
        print("\nBREACH. " + (error_budget(breaches[0]).policy if error_budget(breaches[0]) else ""))
        return 1
    if unmeasured and args.strict:
        keys = ", ".join(r.objective.key for r in unmeasured)
        print(f"\nUNMEASURED (--strict): {keys}")
        return 1
    print("\nAll objectives met.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
