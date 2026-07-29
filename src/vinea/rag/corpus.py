"""The committed corpus: 798 chunks of FAO-56, plus the licence that lets us ship it.

`data/corpus/fao56-chunks.jsonl` is committed for the same reason the two weather
CSVs are: the suite runs offline and the numbers in the docs are checkable.
`scripts/fetch_corpus.py` regenerates it, and refuses to write if FAO's repository
ever stops reporting CC BY 4.0 — an attribution claim that lives only in a
Markdown file is a claim nobody rechecks.

The file's first line is a `source` record, not a chunk. That is deliberate: the
provenance travels *with* the data rather than beside it, so a copy of this file
on its own still says where it came from and under what terms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from vinea import config

CORPUS_PATH = config.DEFAULT_DATA_DIR / "corpus" / "fao56-chunks.jsonl"


@dataclass(frozen=True, slots=True)
class CorpusSource:
    """Where the corpus came from and under what licence. Shipped inside the file."""

    title: str
    issued: str
    licence: str
    doi: str
    citation: str


@dataclass(frozen=True, slots=True)
class Chunk:
    """One passage, with enough locator to be checkable.

    `locator` is the load-bearing field. A passage saying "0.70" with no
    indication that it is Chapter 6, mid-season, is a citation nobody can check —
    which is worse than no citation, because it moves a claim from *unverified*
    to *falsely verified*.
    """

    id: int
    chapter: str
    section: str
    locator: str
    text: str

    @property
    def embedding_text(self) -> str:
        """What actually gets embedded: the locator, then the passage.

        The heading carries topic words the body often omits — a paragraph deep
        inside the soil-water chapter may never repeat the phrase "soil water
        stress" — so prepending it measurably improves recall for queries phrased
        in the document's own vocabulary.
        """
        return f"{self.locator}. {self.text}"


@lru_cache(maxsize=1)
def load_corpus(path: Path | None = None) -> tuple[Chunk, ...]:
    """Every chunk, in file order. Cached — this is a read-only committed file."""
    target = path or CORPUS_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"corpus not found at {target}. Regenerate it with "
            "`uv run python scripts/fetch_corpus.py`."
        )
    chunks: list[Chunk] = []
    with target.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("kind") != "chunk":
                continue
            chunks.append(
                Chunk(
                    id=record["id"],
                    chapter=record["chapter"],
                    section=record.get("section", ""),
                    locator=record["locator"],
                    text=record["text"],
                )
            )
    return tuple(chunks)


@lru_cache(maxsize=1)
def load_source(path: Path | None = None) -> CorpusSource:
    """The provenance record from the head of the file."""
    target = path or CORPUS_PATH
    with target.open(encoding="utf-8") as handle:
        record = json.loads(handle.readline())
    if record.get("kind") != "source":
        raise ValueError(f"{target} does not start with a source record; regenerate it")
    return CorpusSource(
        title=record["title"],
        issued=record["issued"],
        licence=record["licence"],
        doi=record["doi"],
        citation=record["citation"],
    )
