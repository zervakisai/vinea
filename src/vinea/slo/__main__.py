"""`python -m vinea.slo` -- report the objectives, or check them and exit non-zero.

    python -m vinea.slo report             # the table, for a human
    python -m vinea.slo check              # exit 1 if any objective is unmet
    python -m vinea.slo check --strict     # ...or if any cannot be measured
    python -m vinea.slo check --no-notify  # ...without telling the channel
    python -m vinea.slo check --no-record  # ...and without writing rows either

`check` is the SLO equivalent of `alembic check`: one question, one exit code,
usable by a person, by cron, or by CI. Beyond the exit code it writes a row per
unmet objective into `slo_breaches`, so *how long have we been in breach* becomes
answerable -- a live query can only say whether we are -- and posts the breaches to
`VINEA_ALERT_WEBHOOK_URL` if one is set.

The webhook is off unless configured, and that is the gate that matters: a laptop
has no `VINEA_ALERT_WEBHOOK_URL`, so running this while debugging posts nothing.
`--no-notify` is for the case the environment cannot decide -- debugging inside the
cluster, where the variable is present and the channel does not need to hear about
it five times.

`--strict` exists because an objective that cannot be measured is not the same as
one that is met, and the two need different exit codes for different callers. A
cron job wants to know about breaches; a release gate wants to know that the
measurement is working at all. **`--strict` does not notify**: "the query returned
no rows" is a message to whoever ran the gate, not to a channel that is watching
for grower-visible problems.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlmodel import Session

from vinea.db.session import make_engine, scope_to_ops
from vinea.slo import notify
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
                "--no-record",
                action="store_true",
                help="Report breaches without writing rows. Implies --no-notify.",
            )
            parser_for.add_argument(
                "--no-notify",
                action="store_true",
                help="Do not post to VINEA_ALERT_WEBHOOK_URL even if it is set.",
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

    # After the commit, and outside the session block: the row is the record, the
    # message is a courtesy, and a courtesy must not be able to hold a transaction
    # open for the length of a network timeout. If the process dies between the two,
    # the breach is still on disk -- the ordering that survives a crash correctly.
    #
    # `--no-record` implies `--no-notify`: announcing a breach you have explicitly
    # declined to keep a record of is the one combination that helps nobody, since
    # the channel is told about something no query can later confirm happened. The
    # `report` check comes first because the two flags only exist on `check`, and
    # `or` short-circuits before touching them.
    silent = args.command != "check" or args.no_notify or args.no_record
    if breaches and not silent:
        if notify.webhook_url() is None:
            print("\nno VINEA_ALERT_WEBHOOK_URL set; nobody was notified")
        elif notify.notify(results):
            print(f"\nnotified {len(breaches)} breach(es)")
        else:
            # Deliberately not an error. See notify.py: a webhook that is down must
            # not change the exit code, or the exit code stops meaning "breach".
            print("\nWARNING: the alert webhook did not accept the post; see the log")

    if breaches:
        # The policy comes from whichever breach has a budget, not from the first
        # one: latency has no error budget by design (ADR-010 -- "p95 under 300 ms"
        # does not decompose into a countable number of bad events), so a latency
        # breach used to print a bare "BREACH." with the ship/stop decision missing
        # exactly when a second, budgeted breach was sitting in the same list.
        budgets = [b for b in (error_budget(r) for r in breaches) if b is not None]
        if budgets:
            # An exhausted budget outranks a remaining one: "stop shipping" and
            # "ship" cannot both be the answer, and the stricter one is the answer.
            spent = next((b for b in budgets if b.exhausted), budgets[0])
            print("\nBREACH. " + spent.policy)
        else:
            print("\nBREACH.")
        return 1
    if unmeasured and args.strict:
        keys = ", ".join(r.objective.key for r in unmeasured)
        print(f"\nUNMEASURED (--strict): {keys}")
        return 1
    print("\nAll objectives met.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
