#!/usr/bin/env python3
"""Regenerate the committed retrieval corpus from FAO's knowledge repository.

    uv run python scripts/fetch_corpus.py            # rewrite data/corpus/ in place
    uv run python scripts/fetch_corpus.py --dry-run  # print the summary only

Same reasoning as `fetch_dataset.py`: the corpus is committed so the suite runs
offline and the numbers in the docs are checkable, and committed data rots into a
mystery unless the exact call that produced it is in the repo, runnable.

The source is **FAO Irrigation and Drainage Paper 56, *Crop evapotranspiration*,
second edition revised 2025** -- the document `features.py` implements. Grounding
an FAO-56 implementation in FAO-56 is the point, and it is also the phase's whole
difficulty: this text contains the constants the code computes with, and nothing
retrieved from it is ever allowed to reach a computation.

Two things this script does that a `curl` would not:

  **It verifies the licence before writing.** The handle is resolved through the
  repository API and `dc.rights.license` is asserted to be CC BY 4.0. An
  attribution claim that lives only in a Markdown file is a claim nobody rechecks;
  this one fails the regeneration if it ever stops being true.

  **It uses FAO's own extracted text, not the PDF.** The repository publishes a
  TEXT bitstream (~1 MB) beside the 22 MB PDF, so there is no PDF parser in this
  project and no chance of our extraction differing from theirs.

That extraction is faithful to the *page*, not to the prose -- hard-wrapped at
about 53 characters, 175 blank lines in 19 000, equations flattened into symbol
soup, figure axis labels inline as though they were sentences. The chunker below
is mostly about that, and `_is_prose` is the load-bearing part of it.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "data" / "corpus"
OUT_PATH = OUT_DIR / "fao56-chunks.jsonl"

API = "https://openknowledge.fao.org/server/api"
HANDLE = "hdl:20.500.14283/cd6621en"
EXPECTED_LICENCE = "CC BY 4.0"
DOI = "https://doi.org/10.4060/cd6621en"
CITATION = (
    "FAO. 2025. Crop evapotranspiration - Guidelines for computing crop water "
    "requirements. Second edition, revised 2025. FAO Irrigation and Drainage "
    "Paper No. 56 Rev.1. Rome, FAO."
)

# A user agent is required; the repository answers 403 to the default one.
_UA = {"User-Agent": "vinea-corpus-fetch/1.0 (+https://github.com/zervakisai/vinea)"}

# Chunk geometry. ~1200 characters is roughly 300 tokens: large enough that a
# retrieved passage carries its own argument, small enough that a citation points
# at something a person can actually read before deciding whether they agree.
CHUNK_CHARS = 1200
OVERLAP_CHARS = 200

# Sanity floor/ceiling. Like fetch_dataset.py refusing to write on the wrong row
# count: a chunker that silently produces 57 chunks from a 300-page book has
# failed, and the failure is invisible until retrieval quietly returns nothing
# useful. (57 is not hypothetical -- it is what the first attempt produced.)
MIN_CHUNKS = 400
MAX_CHUNKS = 4000

# Chapter detection, and the first version of this got it wrong in a way worth
# keeping visible. `^Chapter\s+(\d+)\b\s*[-–—]?\s*(.*)$` looks reasonable and
# matches the *sentence* "Chapter 1 describes the primary methods for measuring
# actual ETc..." -- so a prose line became a heading, and every chunk after it
# was stamped "Chapter 1" until the next false match. Chunks about the soil water
# balance shipped labelled as Chapter 1. A wrong locator is worse than none: it
# makes a claim look checkable and sends the checker to the wrong page.
#
# The document's real structure, once you look at it rather than guess:
#   * the preface lists every chapter as `Chapter 7 - ETc and dual crop ...:`
#   * the body marks each chapter start with a BARE `Chapter 7` line
# So titles come from the preface, and boundaries from the bare lines.
_CHAPTER_TITLE = re.compile(r"^Chapter\s+(\d+)\s*[-–—]\s*(\S.*?):?\s*$")
_CHAPTER_START = re.compile(r"^Chapter\s*(\d+)\s*$")
_MARKER = re.compile(r"^(TABLE|FIGURE|BOX|EXAMPLE)\s+(\d+(?:\.\d+)?)\b\s*(.*)$")
_ALLCAPS = re.compile(r"^[A-Z][A-Z0-9 ,\-/()]{6,}$")
# A table-of-contents line: heading text ending in a page number.
_TOC = re.compile(r"\s\d{1,3}\s*$")
# A bibliography entry: "Allen, R.G., Pruitt, W.O., 1991. Lysimeters..."
#
# These pass `_is_prose` easily and retrieve *well* -- they are dense with exactly
# the terms a query uses and contain no argument at all. Before this filter, the
# top hit for "wind speed measurement height and its effect on reference
# evapotranspiration" was a reference list. Retrieval quality is often a corpus
# problem wearing an embedding problem's clothes.
_BIBLIOGRAPHY = re.compile(r"[A-Z][a-z]+,\s*[A-Z]\.")


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise SystemExit(f"FAO repository returned HTTP {response.status} for {url}")
        return json.load(response)


def resolve_item() -> dict:
    """The item's metadata, with the licence checked rather than assumed."""
    item = _get_json(f"{API}/pid/find?id={HANDLE}")
    metadata = item.get("metadata", {})

    def one(key: str) -> str | None:
        values = metadata.get(key) or []
        return values[0]["value"] if values else None

    licence = one("dc.rights.license")
    if licence != EXPECTED_LICENCE:
        # Refuse loudly. Attribution is a promise this repository makes in
        # data/ATTRIBUTION.md, and a promise nobody rechecks is a promise that
        # expires quietly.
        raise SystemExit(
            f"licence changed: expected {EXPECTED_LICENCE!r}, repository now says {licence!r}. "
            "Update data/ATTRIBUTION.md and this script deliberately, or stop redistributing."
        )
    return {
        "uuid": item["uuid"],
        "title": item.get("name"),
        "issued": one("dc.date.issued"),
        "licence": licence,
        "doi": one("fao.identifier.doi") or DOI,
    }


def fetch_text(uuid: str) -> str:
    """FAO's own extracted plain text for the item (the TEXT bundle)."""
    bundles = _get_json(f"{API}/core/items/{uuid}/bundles")["_embedded"]["bundles"]
    text_bundle = next((b for b in bundles if b["name"] == "TEXT"), None)
    if text_bundle is None:
        raise SystemExit("no TEXT bundle on the item; FAO may have stopped publishing extracted text")

    bitstreams = _get_json(f"{API}/core/bundles/{text_bundle['uuid']}/bitstreams")
    bitstream = bitstreams["_embedded"]["bitstreams"][0]
    request = urllib.request.Request(bitstream["_links"]["content"]["href"], headers=_UA)
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read().decode("utf-8", errors="replace")


def _is_prose(line: str) -> bool:
    """Does this line read like a sentence, or like a flattened equation?

    The heuristic that makes the corpus usable. PDF text extraction turns

        K  = −− −−− −−s −al−t  −− −−r, − is − ) ( 1- −− −−b− −−
        0.0 RAWsalt RAW TAWsalt TAW

    into "text", and an embedding of that competes for retrieval slots against
    the paragraph that actually explains RAW. Two cheap tests catch nearly all of
    it: enough words, and enough of the characters being letters or spaces.

    Deliberately *not* a parser. The goal is a corpus where the good passages win,
    not a faithful reconstruction of the document -- and a heuristic that drops a
    little real text is far cheaper here than one that admits equation debris.
    """
    stripped = line.strip()
    if len(stripped) < 25:
        return False
    words = [w for w in stripped.split() if len(w) > 2 and w[0].isalpha()]
    if len(words) < 5:
        return False
    alpha = sum(1 for c in stripped if c.isalpha() or c.isspace())
    return alpha / len(stripped) >= 0.75


def chapter_titles(lines: list[str]) -> dict[int, str]:
    """Chapter number -> title, read from the preface's own list of chapters.

    The document tells us its structure; the alternative is inferring titles from
    whatever ALLCAPS line happens to follow a chapter break, which is how the
    first attempt ended up captioning the water-balance chapter "Chapter 1".
    """
    titles: dict[int, str] = {}
    for line in lines:
        match = _CHAPTER_TITLE.match(line.strip())
        if match:
            number, title = match.groups()
            titles.setdefault(int(number), title.strip())
    return titles


def _body_start(lines: list[str]) -> int:
    """Index of the first bare `Chapter N` line, i.e. where the body begins.

    Everything above it is cover, table of contents and preface. The TOC repeats
    every heading in the book with a page number attached, and navigation
    furniture retrieves *well* while saying nothing -- so it is dropped wholesale
    rather than filtered chunk by chunk.
    """
    for index, line in enumerate(lines):
        if _CHAPTER_START.match(line.strip()):
            return index
    return 0


def chunk(text: str) -> list[dict]:
    """Section-aware chunks, each stamped with where it came from.

    The locator is the point. A passage saying "0.70" with no indication that it
    is Table 12, mid-season, wine grapes is a citation nobody can check -- which
    is worse than no citation, because it moves a claim from *unverified* to
    *falsely verified*.
    """
    lines = text.split("\n")
    titles = chapter_titles(lines)
    lines = lines[_body_start(lines) :]

    chunks: list[dict] = []
    chapter = "Chapter 1"
    section = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        body = " ".join(buffer)
        buffer = []
        start = 0
        while start < len(body):
            piece = body[start : start + CHUNK_CHARS]
            if len(piece) >= 200:
                chunks.append(
                    {
                        "chapter": chapter,
                        "section": section,
                        "locator": f"{chapter}{' — ' + section if section else ''}",
                        "text": piece.strip(),
                    }
                )
            if start + CHUNK_CHARS >= len(body):
                break
            start += CHUNK_CHARS - OVERLAP_CHARS

    for raw in lines:
        line = raw.strip()

        chapter_match = _CHAPTER_START.match(line)
        if chapter_match:
            flush()
            number = int(chapter_match.group(1))
            title = titles.get(number, "")
            chapter = f"Chapter {number}" + (f" — {title}" if title else "")
            section = ""
            continue

        marker_match = _MARKER.match(line)
        if marker_match:
            flush()
            kind, number, title = marker_match.groups()
            section = f"{kind.title()} {number}" + (f": {title.strip()}" if title.strip() else "")
            continue

        if _ALLCAPS.match(line) and not _TOC.search(line):
            flush()
            section = line.title()
            continue

        if _is_prose(line) and not _BIBLIOGRAPHY.search(line):
            buffer.append(line)
        # Anything else -- equation debris, axis labels, page numbers -- is
        # dropped rather than buffered. See `_is_prose`.

    flush()
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the summary, write nothing")
    args = parser.parse_args()

    item = resolve_item()
    print(f"source   : {item['title']} ({item['issued']})")
    print(f"licence  : {item['licence']}   doi: {item['doi']}")

    text = fetch_text(item["uuid"])
    print(f"text     : {len(text):,} chars, {text.count(chr(10)):,} lines")

    chunks = chunk(text)
    chapters = sorted({c["chapter"] for c in chunks})
    mean = sum(len(c["text"]) for c in chunks) / max(len(chunks), 1)
    print(f"chunks   : {len(chunks):,} across {len(chapters)} chapters, mean {mean:.0f} chars")

    if not MIN_CHUNKS <= len(chunks) <= MAX_CHUNKS:
        raise SystemExit(
            f"refusing to write {len(chunks)} chunks (expected {MIN_CHUNKS}-{MAX_CHUNKS}). "
            "The extraction or the chunker changed shape; look before overwriting."
        )

    if args.dry_run:
        print("\n--- dry run, nothing written ---")
        for sample in chunks[:2]:
            print(f"\n[{sample['locator']}]\n{sample['text'][:280]}...")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as handle:
        # First line is provenance, not a chunk. Readers skip `kind == "source"`.
        handle.write(
            json.dumps(
                {
                    "kind": "source",
                    "title": item["title"],
                    "issued": item["issued"],
                    "licence": item["licence"],
                    "doi": item["doi"],
                    "citation": CITATION,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        for index, piece in enumerate(chunks):
            handle.write(json.dumps({"kind": "chunk", "id": index, **piece}, ensure_ascii=False) + "\n")

    print(f"wrote    : {OUT_PATH.relative_to(REPO_ROOT)}  ({OUT_PATH.stat().st_size/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
