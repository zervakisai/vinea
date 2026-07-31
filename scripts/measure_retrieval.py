"""Compare retrieval variants over the labelled questions. The floors come from here.

    uv run python scripts/measure_retrieval.py            # the table
    uv run python scripts/measure_retrieval.py --misses   # ...and what each got wrong

Every retrieval number in this repository -- the floors in `tests/test_rag.py`, the
tables in `rag/store.py` and ADR-011 -- is produced by this script. It exists
because the alternative is a comment saying "measured 0.78", which cannot be re-run,
cannot be checked, and quietly becomes false the first time anything changes.

The arithmetic lives in `vinea.evals.retrieval`, which the recall gate also uses, so
the harness and the gate cannot drift apart.

## Read the paraphrase row

The first twelve questions are in FAO-56's own vocabulary and were written alongside
the chunker, which is why they scored 1.00 and measured nothing. The fifteen
prefixed `p-` ask the same things the way a grower asks them. **A variant that
improves the overall number by improving the easy half has improved nothing.**

That is not hypothetical: adding the locator to the tsvector takes the original
twelve to a perfect score and costs two of the paraphrases.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import text as sql
from sqlmodel import Session

from vinea.db.session import make_engine
from vinea.evals.retrieval import DEFAULT_DEPTH, RetrievalScore, score_ranked_results
from vinea.rag import corpus
from vinea.rag.store import ingest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "rag_questions.json"

# A scratch source, so a run never disturbs the corpus an application is serving.
SOURCE = "measure-retrieval"

TEXT = "to_tsvector('english', c.text)"
LOCATOR_AND_TEXT = "to_tsvector('english', c.locator || '. ' || c.text)"

# `ts_rank_cd`'s third argument is a normalisation bitmask. 0 is raw density, which
# is length-biased: a long chunk outranks a short one for repeating the same words.
# 2 divides by document length -- the correction BM25 exists to make.
#
# The losing variants stay in the table on purpose. "We tried the obvious thing and
# it was worse" is only credible if the obvious thing can still be run.
CANDIDATES: dict[str, tuple[str, int]] = {
    "text, no normalisation": (TEXT, 0),
    "text, /length": (TEXT, 2),  # <- what ships
    "text, /(1+log length)": (TEXT, 1),
    "text, /unique words": (TEXT, 8),
    "locator+text, no normalisation": (LOCATOR_AND_TEXT, 0),
    "locator+text, /length": (LOCATOR_AND_TEXT, 2),
}


def statement(vector: str, normalisation: int) -> str:
    """The shipped query with the vector and normalisation swapped out.

    Kept textually close to `rag/store.py`. A harness that measures a query the
    application does not run produces numbers about the harness.
    """
    return f"""
WITH tsq AS (
    SELECT replace(plainto_tsquery('english', :query_text)::text, '&', '|')::tsquery AS query
)
SELECT c.locator, ts_rank_cd({vector}, tsq.query, {normalisation}) AS score
FROM corpus_chunks AS c, tsq
WHERE c.source = :source AND {vector} @@ tsq.query
ORDER BY score DESC, c.id
LIMIT {DEFAULT_DEPTH}
"""


def run_variant(session: Session, stmt: str, questions: list[dict]) -> RetrievalScore:
    results, wanted = [], []
    for question in questions:
        rows = session.execute(
            sql(stmt), {"query_text": question["query"], "source": SOURCE}
        ).all()
        results.append((question["id"], [row[0] for row in rows]))
        wanted.append(set(question["chapters"]))
    return score_ranked_results(results, wanted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--misses", action="store_true", help="List what each variant put outside the top 3."
    )
    args = parser.parse_args(argv)

    questions = json.loads(FIXTURE.read_text())["questions"]
    paraphrase = [q for q in questions if q["id"].startswith("p-")]
    original = [q for q in questions if not q["id"].startswith("p-")]
    print(
        f"{len(questions)} questions: {len(original)} in the manual's vocabulary, "
        f"{len(paraphrase)} paraphrased\n"
    )

    with Session(make_engine()) as session:
        session.execute(sql("DELETE FROM corpus_chunks WHERE source = :s"), {"s": SOURCE})
        written = ingest(session, corpus.load_corpus(), source=SOURCE)
        session.commit()
        print(f"ingested {written} chunks\n")

        header = f"{'variant':<32} {'set':<11} {'r@1':>6} {'r@3':>6} {'r@5':>6} {'MRR':>7}"
        print(header)
        print("-" * len(header))
        for name, (vector, normalisation) in CANDIDATES.items():
            stmt = statement(vector, normalisation)
            for label, subset in (("all", questions), ("paraphrase", paraphrase)):
                score = run_variant(session, stmt, subset)
                print(
                    f"{name:<32} {label:<11} {score.recall_at_1:>6.2f} "
                    f"{score.recall_at_3:>6.2f} {score.recall_at_5:>6.2f} {score.mrr:>7.3f}"
                )
                if args.misses and score.outside_top_3:
                    print(f"{'':<45}outside top-3: {', '.join(score.outside_top_3)}")
            print()

        # Leave nothing behind. This scratch source shares a table with the corpus the
        # application serves, and a stray 798 rows would quietly join every search.
        session.execute(sql("DELETE FROM corpus_chunks WHERE source = :s"), {"s": SOURCE})
        session.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
