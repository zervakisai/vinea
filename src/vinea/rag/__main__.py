"""`python -m vinea.rag` -- ingest the corpus, or ask it a question.

Two commands, and they are the operational half of ADR-008: the index is a table
in the database we already run, so filling it is a job you run rather than a
service you provision.

    python -m vinea.rag ingest              # embed the committed corpus into Postgres
    python -m vinea.rag search "Kc grapes"  # what would an agent be shown?

`search` exists because the alternative way to find out why a passage was chosen
is to read a nightly advisory and guess. Retrieval quality is the kind of thing
that degrades one chunking change at a time, and a command that shows the actual
ranked output is what makes that visible before the eval gate has to catch it.
"""

from __future__ import annotations

import argparse
import sys

from sqlmodel import Session

from vinea.db.session import make_engine
from vinea.rag.corpus import load_corpus, load_source
from vinea.rag.retrieve import SOURCE
from vinea.rag.store import ingest, search


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vinea.rag", description="Corpus ingest and retrieval.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Embed the committed corpus into corpus_chunks.")
    p_ingest.add_argument("--source", default=SOURCE)

    p_search = sub.add_parser("search", help="Run one hybrid query and print the ranked passages.")
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=int, default=5)
    p_search.add_argument("--source", default=SOURCE)

    args = parser.parse_args(argv)
    engine = make_engine()

    if args.command == "ingest":
        source_record = load_source()
        chunks = load_corpus()
        print(f"corpus  : {source_record.title} ({source_record.issued}), {source_record.licence}")
        print(f"chunks  : {len(chunks)}")
        with Session(engine) as session:
            written = ingest(session, chunks, source=args.source)
            # The caller owns the transaction everywhere else in this codebase;
            # here the caller IS the command, so this is where the commit belongs.
            session.commit()
        print(f"ingested: {written} rows into corpus_chunks (source={args.source!r})")
        return 0

    with Session(engine) as session:
        hits = search(session, args.query, source=args.source, top_k=args.top_k)
    if not hits:
        print("no results. Has the corpus been ingested? `python -m vinea.rag ingest`")
        return 1
    for rank, hit in enumerate(hits, start=1):
        print(f"\n[{rank}] rank={hit.score:.5f}  {hit.locator}")
        print(f"    {hit.text[:220]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
