"""Comments in `src/` explain the code, not the course it was written for.

This repository began as a teaching sequence and its comments carried that:
references to build phases, section codes from a design document, and narration
about what the author had just worked out. All of it made sense read in order, and
none of it makes sense to someone opening `worker.py` because a task failed at
03:00.

These tests are blunt on purpose. They scan source text for markers that address a
reader who is following a curriculum rather than operating a service. Nothing here
judges prose quality — a linter cannot — so they check only for references to
things a reader of this file does not have.

**ADR references are allowed and encouraged.** The ADRs are in this repository,
they carry the arguments, and `(ADR-003)` lets a comment state a constraint
without restating a page of reasoning. That is the difference between a citation
and a private code.

Offline, no database, no network. `CONTRIBUTING.md` states the rule these enforce.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# Markers that address a curriculum reader. Each maps to what to do instead.
FORBIDDEN = {
    # "phase 14 added this" -- the reader is not reading phases; git log has it.
    "build-phase reference": (
        re.compile(r"\bphases?\s+\d", re.IGNORECASE),
        "state the reason inline, or cite an ADR; the phase history is in git and docs/",
    ),
    # "S4.3", "S3.5" -- section codes from a design document.
    "design-doc section code": (
        re.compile(r"\bS\d\.\d\b"),
        "say what the constraint is; nobody opening this file has the section numbering",
    ),
    # "B2", "B3-4" -- workstream codes from the same document.
    "workstream code": (
        re.compile(r"\bB\d(?:-\d)?\b(?!\s*[a-z])"),
        "name the concern instead of its code",
    ),
    "reference to DESIGN.md": (
        re.compile(r"DESIGN\.md"),
        "point at an ADR, or state the reason inline",
    ),
}

# Files exempt, and each exemption is a decision rather than an oversight.
EXEMPT = {
    # The engineering log's own module, if one is ever added, would legitimately
    # talk about phases. Nothing qualifies today; the set is here so an exemption
    # has to be added deliberately and reviewed.
}


def _source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name not in EXEMPT)


def test_source_files_exist():
    """Guard on the guard: a glob that matches nothing passes every other test."""
    files = _source_files()
    assert len(files) > 30, f"only found {len(files)} source files; the scan path is wrong"


def test_no_curriculum_references_in_source():
    """The rule from CONTRIBUTING.md, enforced.

    Failure output is grouped by marker with file:line, so the fix is mechanical:
    read the line, work out what it was trying to say, and say that instead.
    """
    offenders: dict[str, list[str]] = {}

    for path in _source_files():
        text = path.read_text()
        rel = path.relative_to(SRC.parent.parent)
        for name, (pattern, _advice) in FORBIDDEN.items():
            # Scanned over the WHOLE text, not line by line. `\s` matches a
            # newline, so "…as it did in phase\n13…" is a real match that a
            # line-based scan silently misses -- and one did survive that way.
            for match in pattern.finditer(text):
                lineno = text.count("\n", 0, match.start()) + 1
                excerpt = " ".join(text[match.start() : match.end() + 60].split())
                offenders.setdefault(name, []).append(f"{rel}:{lineno}: …{excerpt[:88]}")

    if offenders:
        report = []
        for name, hits in sorted(offenders.items()):
            report.append(f"\n{name} ({len(hits)}) -- {FORBIDDEN[name][1]}")
            report.extend(f"    {hit}" for hit in hits[:12])
            if len(hits) > 12:
                report.append(f"    ... and {len(hits) - 12} more")
        raise AssertionError("\n".join(report))


def test_adr_references_are_allowed():
    """The exception that makes the rule usable.

    An ADR reference is a citation to a document in this repository, not a private
    code. If this ever starts failing, the pattern for one of the forbidden markers
    has grown teeth it should not have.
    """
    sample = "# One replica: the response cache is in-process (ADR-003, ADR-011)."
    for pattern, _ in FORBIDDEN.values():
        assert not pattern.search(sample), f"{pattern.pattern} matched an ADR citation"


def test_the_forbidden_patterns_actually_match_what_they_claim_to():
    """A regex that matches nothing is a test that always passes.

    Each pattern is checked against the text it exists to catch. Without this the
    suite would go green the day someone tightened a pattern into uselessness.
    """
    cases = {
        "build-phase reference": ["# phase 14 added this", "# Phase 8 built the queue", "# phases 1-4"],
        "design-doc section code": ["# see S4.3", "# S3.5's degraded path"],
        "workstream code": ["# B2's circularity trap", "# B3-4 typed variables"],
        "reference to DESIGN.md": ["# DESIGN.md B1 argues"],
    }
    for name, samples in cases.items():
        pattern = FORBIDDEN[name][0]
        for sample in samples:
            assert pattern.search(sample), f"{name} failed to match {sample!r}"
