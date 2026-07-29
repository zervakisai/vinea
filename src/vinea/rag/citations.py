"""What retrieval put in front of the model during one advisory run.

The same shape as phase 14's cost ledger, and reused for the same reason: the
thing worth recording is produced deep inside a run, by code the graph does not
know about, and has to be readable synchronously the moment the run ends so the
worker can write it in the same transaction as the advisory.

A `ContextVar` holding a *mutable* object, so children that `asyncio.gather`
inherit the same list by reference — the trick `capture_run_messages` uses.

What is recorded is deliberately narrow: the passages the retriever **supplied**,
per leg, in rank order. Not the passages the model says it used. A model's claim
about its own sources is a self-report, and phase 12 exists because self-report
is not evidence.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RetrievedPassage:
    """One passage handed to one agent, with everything a reader needs to check it."""

    leg: str  # 'irrigation' | 'spray' | 'reconciliation'
    chunk_id: int
    locator: str
    text: str
    rank: int


@dataclass
class CitationLedger:
    """Append-only tally for one advisory run."""

    passages: list[RetrievedPassage] = field(default_factory=list)

    def record(self, passages: list[RetrievedPassage]) -> None:
        self.passages.extend(passages)

    def for_leg(self, leg: str) -> list[RetrievedPassage]:
        return [p for p in self.passages if p.leg == leg]


_ledger: ContextVar[CitationLedger | None] = ContextVar("vinea_citation_ledger", default=None)


def current_citations() -> CitationLedger | None:
    """The ledger for the run in progress, or None outside a scope.

    None is the normal case for a CLI run or a unit test: nothing is collecting,
    so `record` becomes a no-op and retrieval still works. A retrieval layer that
    required a ledger to be open would make every caller responsible for
    bookkeeping it does not care about.
    """
    return _ledger.get()


@contextmanager
def citation_scope() -> Iterator[CitationLedger]:
    """Collect retrieved passages for everything inside this block.

    One advisory, one scope. The reset in `finally` is what stops a worker's
    second task from inheriting the first task's citations — which would attach
    one grower's sources to another grower's advisory.
    """
    ledger = CitationLedger()
    token = _ledger.set(ledger)
    try:
        yield ledger
    finally:
        _ledger.reset(token)
