"""`python -m vinea.context` -- what is actually in the prompt, right now.

    python -m vinea.context                 # both legs, on the committed dataset
    python -m vinea.context --calibrate     # the measured chars-per-token, if any

Exists because phase 15 tripled the irrigation leg's context in one commit and
nothing in the system could see it. A command that prints the table is what turns
"the prompt got big" from a discovery into a number you can check before opening
a pull request.

Runs offline. If the corpus is not ingested, the retrieved-passages row is zero
and the report says so by arithmetic -- which is itself the useful reading, since
that is the shape of every deployment without retrieval.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from vinea import agents, config
from vinea.context.accounting import CHARS_PER_TOKEN, calibration_ratio, report_for_legs
from vinea.deps import WINE_GRAPES
from vinea.features import build_features
from vinea.ingest import load_weather


def _legs(run_date: date) -> dict[str, dict[str, str]]:
    from vinea.rag.queries import irrigation_query, spray_query
    from vinea.rag.retrieve import render_passages, retrieve_for

    data_dir = Path(config.DEFAULT_DATA_DIR)
    history = sorted(data_dir.glob("*last-30d*.csv"))[-1]
    forecast = sorted(data_dir.glob("*next-7d*.csv"))[-1]
    hist, fc, dq = load_weather(history, forecast, run_date)
    features = build_features(hist, fc, dq, run_date, WINE_GRAPES)

    irr_deps = agents.IrrDeps(
        crop=WINE_GRAPES, features=features.irrigation, data_quality=dq,
        target_date=features.target_date, run_date=run_date,
    )
    spray_deps = agents.SprayDeps(
        crop=WINE_GRAPES, features=features.spray, data_quality=dq,
        target_date=features.target_date, run_date=run_date,
    )
    return {
        "irrigation": {
            "static instructions": agents._IRR_STATIC,
            "context block": agents.render_irrigation_context(irr_deps),
            "user input": agents.render_irrigation_input(features.irrigation, features.target_date),
            "retrieved passages": render_passages(retrieve_for("irrigation", irrigation_query(features.irrigation))),
        },
        "spray": {
            "static instructions": agents._SPRAY_STATIC,
            "context block": agents.render_spray_context(spray_deps),
            "user input": agents.render_spray_input(features.spray),
            "retrieved passages": render_passages(retrieve_for("spray", spray_query(features.spray))),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vinea.context", description=__doc__)
    parser.add_argument("--run-date", type=date.fromisoformat, default=date(2026, 7, 28))
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Report the measured chars-per-token from advisories that were metered.",
    )
    args = parser.parse_args(argv)

    if args.calibrate:
        from sqlmodel import Session

        from vinea.db.session import make_engine

        with Session(make_engine()) as session:
            ratio = calibration_ratio(session)
        if ratio is None:
            print(
                "No advisory carries both context_chars and input_tokens, so there is\n"
                "nothing to calibrate against. Both are written together by MeteredModel,\n"
                f"which only wraps the gateway path -- so the estimator is still the\n"
                f"stated assumption of {CHARS_PER_TOKEN} chars/token, unverified."
            )
            return 0
        print(f"measured : {ratio:.2f} chars/token over metered advisories")
        print(f"assumed  : {CHARS_PER_TOKEN:.2f} chars/token")
        if ratio < CHARS_PER_TOKEN:
            print(
                "\nThe text tokenizes DENSER than the assumption, so estimate_tokens\n"
                "under-counts and every budget derived from it is looser than it looks."
            )
        return 0

    for report in report_for_legs(_legs(args.run_date)):
        print(report.as_table())
        share = report.share_of("retrieved passages")
        print(f"retrieved passages: {share * 100:.0f}% of this leg\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
