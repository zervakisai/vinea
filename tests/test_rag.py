"""phase 15 -- the corpus, the hybrid query, the floor, and the line.

Three tiers, and the tier decides what a failure means:

  * **Corpus and query construction** run fully offline against the committed
    JSONL. No database, no network, no model.
  * **The structural invariant** is a source scan: nothing in the deterministic
    core may import `vinea.rag`. It is the strongest guarantee in this file.
  * **Retrieval and the recall gate** need Postgres; they SKIP without one.

Nothing here needs an embedding model, because retrieval no longer uses one
(ADR-011): lexical search alone scored 0.78 recall@3 against the hybrid's 0.70 on
questions phrased the way a grower asks them.
"""

from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

import pytest

from vinea.rag import citations, corpus, retrieve
from vinea.rag.retrieve import REFERENCE_CONTRACT

REPO_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = json.loads((REPO_ROOT / "tests" / "fixtures" / "rag_questions.json").read_text())["questions"]

# The files the invariant protects. Identical to the list in the README's diff
# claim and in every phase doc, on purpose -- one list, several places, and this
# test is the one that would notice if they ever disagreed in practice.
PROTECTED = (
    "features.py",
    "contracts.py",
    "deps.py",
    "graph.py",
    "reconcile.py",
    "pipeline.py",
)


# --------------------------------------------------------------------------- #
# The corpus, and the licence that lets us ship it                             #
# --------------------------------------------------------------------------- #


def test_the_corpus_carries_its_own_licence():
    """Attribution travels inside the file, not beside it.

    `data/ATTRIBUTION.md` can be separated from the data by a single copy-paste.
    The source record is the first line of the corpus itself, so a copy of the
    file on its own still says where it came from and under what terms -- and
    `scripts/fetch_corpus.py` refuses to regenerate if FAO stops saying CC BY 4.0.
    """
    source = corpus.load_source()
    assert source.licence == "CC BY 4.0"
    assert source.doi.startswith("https://doi.org/10.4060/")
    assert "FAO" in source.citation


def test_every_chunk_has_a_checkable_locator():
    """A passage saying "0.70" with no indication of where it is from is worse
    than no passage: it moves a claim from unverified to falsely verified."""
    chunks = corpus.load_corpus()
    assert len(chunks) > 400
    assert all(c.locator.startswith("Chapter ") for c in chunks)
    assert all(c.text.strip() for c in chunks)


def test_the_chunker_dropped_the_table_of_contents_and_the_bibliography():
    """Both retrieve *well* and say nothing, which is the worst combination.

    Before these filters existed, the top hit for a wind-speed query was a list
    of references -- dense with exactly the terms the query used, containing no
    argument at all. Retrieval quality is often a corpus problem wearing an
    embedding problem's clothes.
    """
    text = " ".join(c.text for c in corpus.load_corpus())

    # A bibliography entry: "Allen, R.G., Pruitt, W.O., 1991. Lysimeters..."
    # These were the actual top hit for a wind-speed query before the filter.
    assert "Allen, R.G., Pruitt, W.O." not in text

    # Front matter. The licence boilerplate was the *first* result for
    # "readily available water RAW and management allowed depletion" in the very
    # first measurement -- a paragraph about copyright, retrieved for a question
    # about soil physics, because it was the only chunk the crude chunker kept.
    assert "Under the terms of this licence" not in text
    assert "The designations employed and the presentation" not in text

    # NOT asserted: that the preface's chapter summaries are gone. They survive,
    # and deliberately -- "Chapter 2 - FAO Penman-Monteith ETo equation: ..." is
    # prose describing content, not navigation furniture with a page number
    # attached. The filter targets text that retrieves well and *says nothing*;
    # a summary says something.


def test_chapters_are_attributed_to_the_right_chapter():
    """The regression this exists for is specific and shipped once.

    The first chapter regex matched the *sentence* "Chapter 1 describes the
    primary methods for measuring actual ETc", so a prose line became a heading
    and every chunk after it was stamped Chapter 1 -- including the whole soil
    water balance chapter. A wrong locator sends a checker to the wrong page,
    with full confidence.
    """
    chunks = corpus.load_corpus()
    by_chapter = {}
    for chunk in chunks:
        by_chapter.setdefault(chunk.chapter, []).append(chunk)

    # Chapter 8 is soil water and salinity stress; TAW/RAW live there.
    chapter_8 = next(k for k in by_chapter if k.startswith("Chapter 8"))
    assert "soil water" in chapter_8.lower()
    joined = " ".join(c.text for c in by_chapter[chapter_8]).lower()
    assert "total available water" in joined
    assert "readily available water" in joined

    # And no single chapter swallowed the book.
    assert max(len(v) for v in by_chapter.values()) < len(chunks) * 0.5


# --------------------------------------------------------------------------- #
# The structural invariant -- retrieval may not reach the physics              #
# --------------------------------------------------------------------------- #


def _imports_of(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_protected_core_never_imports_retrieval():
    """The phase's central rule, enforced by scanning the source.

    The corpus is FAO-56, which is where Kc tables and `RAW = TAW x MAD` live.
    The moment a retrieved passage can influence a computed value, the water
    balance has two sources: a config row a human chose, and whatever an
    embedding surfaced tonight. The second is not reproducible, not covered by
    `deps_hash`, and invisible in every drift tag.

    `git diff` proves the protected files did not change *this* phase. This test
    proves they cannot start depending on retrieval in a *later* one.
    """
    offenders: dict[str, set[str]] = {}
    for name in PROTECTED:
        path = REPO_ROOT / "src" / "vinea" / name
        bad = {i for i in _imports_of(path) if "rag" in i.split(".")}
        if bad:
            offenders[name] = bad
    assert not offenders, f"the deterministic core reached into retrieval: {offenders}"


def test_retrieval_runs_after_the_features_are_computed():
    """Call order, not a comment: the agents receive features already built.

    `run_irrigation_agent(crop, features, ...)` is handed a computed
    `IrrigationFeatures`. Retrieval happens inside it, so there is no arrangement
    of this code in which a passage reaches `build_features` -- the features
    exist before retrieval is called.
    """
    source = (REPO_ROOT / "src" / "vinea" / "agents.py").read_text()
    call = source.index("async def run_irrigation_agent")
    body = source[call : source.index("async def run_spray_agent")]
    assert 'retrieve_for("irrigation"' in body
    # The features are a parameter, so they cannot depend on what follows.
    assert "features: IrrigationFeatures" in body


# --------------------------------------------------------------------------- #
# The floor: silence, never a weak citation                                    #
# --------------------------------------------------------------------------- #


def test_retrieval_returns_nothing_and_does_not_raise_without_a_database(monkeypatch):
    """The nightly run must not fail because an *optional* index is unreachable.

    Same instinct as B3's prompt registry: a grower's advisory never errors
    because an auxiliary system is down. The difference is where it lands --
    the registry falls back to a bundled prompt, this falls back to nothing at
    all, because there is no such thing as a safe substitute citation.
    """

    def _no_database(*_args, **_kwargs):
        raise RuntimeError("connection refused")

    # The cache has to be cleared first, and that is the point rather than test
    # bookkeeping: `retrieve_for` reuses an engine across advisories, so a live
    # one from an earlier test would sail straight past this monkeypatch and the
    # test would pass while proving nothing.
    retrieve.reset_engine_cache()
    monkeypatch.setattr("vinea.db.session.make_engine", _no_database)
    try:
        assert retrieve.retrieve_for("irrigation", "readily available water") == []
    finally:
        retrieve.reset_engine_cache()


def test_no_passages_renders_to_an_empty_string():
    """Not "no sources found".

    A sentence about the absence of sources is itself something a model will try
    to be helpful about -- and being helpful about missing citations is how an
    unfounded one gets written.
    """
    assert retrieve.render_passages([]) == ""


def test_rendered_passages_forbid_recomputation_in_the_imperative():
    """The framing is the safeguard, not decoration.

    Without it a model reads retrieved text as additional input data and starts
    arithmetic on any number in it -- which for this corpus means reading a Kc out
    of a table that FAO wrote for a different vineyard.
    """
    passage = citations.RetrievedPassage(
        leg="irrigation", chunk_id=1, locator="Chapter 8", text="RAW = p x TAW", rank=1
    )
    rendered = retrieve.render_passages([passage])
    # Asserted against the exported contract, not the sentence. Rewording the
    # framing must not turn a prose edit into a red build -- that is how an
    # assertion gets deleted instead of the risk it guards.
    assert all(rule in rendered for rule in REFERENCE_CONTRACT)
    assert "CC BY 4.0" in rendered  # the licence travels with the quoted text
    assert "Chapter 8" in rendered


def test_the_citation_ledger_does_not_leak_between_runs():
    """Two tasks in one worker process must not share citations -- that would
    attach one grower's sources to another grower's advisory."""
    with citations.citation_scope() as first:
        first.record([citations.RetrievedPassage("irrigation", 1, "Chapter 8", "x", 1)])
        assert len(first.passages) == 1
    with citations.citation_scope() as second:
        assert second.passages == []


def test_recording_outside_a_scope_is_a_no_op(monkeypatch):
    """A CLI run retrieves without anyone collecting, and that must be fine."""
    assert citations.current_citations() is None


# --------------------------------------------------------------------------- #
# Retrieval against a real database                                            #
# --------------------------------------------------------------------------- #

pytestmark_db = pytest.mark.db


@pytest.fixture
def ingested(db_session):
    """A small slice of the corpus, embedded with the stub, in a rolled-back tx.

    The stub is correct here: these tests assert that the *query* works -- that
    both halves run, that fusion orders results, that the source filter holds.
    None of that depends on the vectors meaning anything, and using the real
    model would make a plumbing test depend on a download.
    """
    from vinea.rag.store import ingest

    chunks = corpus.load_corpus()[:120]
    ingest(db_session, chunks, source="test-fao56")
    return db_session


@pytest.mark.db
def test_hybrid_search_returns_ranked_hits(ingested):
    from vinea.rag.store import search

    hits = search(ingested, "evapotranspiration", source="test-fao56", top_k=5)
    assert hits
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    assert all(h.locator.startswith("Chapter") for h in hits)


@pytest.mark.db
def test_the_lexical_half_works_without_any_embedding(ingested):
    """Half the hybrid needs no model at all, which is what makes the recall gate
    runnable in CI without a secret -- and what covers exact-token queries like
    `Kc` or `ETo`, where a 256-dimension static embedding is weakest."""
    from sqlalchemy import text as sql

    from vinea.rag.store import search

    ingested.execute(sql("UPDATE corpus_chunks SET embedding = NULL WHERE source = 'test-fao56'"))
    hits = search(ingested, "evapotranspiration", source="test-fao56", top_k=3)
    assert hits, "lexical retrieval must survive an unembedded corpus"


@pytest.mark.db
def test_search_never_crosses_a_source_boundary(ingested):
    from vinea.rag.store import search

    assert search(ingested, "evapotranspiration", source="other", top_k=5) == []


@pytest.mark.db
def test_reingesting_the_same_corpus_overwrites_rather_than_duplicates(db_session):
    """The same idempotency shape as the advisory upsert, for the same reason:
    this will be run twice by someone unsure whether the first run worked."""
    from sqlalchemy import func, select

    from vinea.db.models import CorpusChunk
    from vinea.rag.store import ingest

    chunks = corpus.load_corpus()[:40]
    ingest(db_session, chunks, source="test-twice")
    ingest(db_session, chunks, source="test-twice")
    count = db_session.execute(
        select(func.count()).select_from(CorpusChunk).where(CorpusChunk.source == "test-twice")
    ).scalar_one()
    assert count == 40


# --------------------------------------------------------------------------- #
# The recall gate                                                              #
# --------------------------------------------------------------------------- #


# The floors the gate holds, measured 2026-07-30 over 27 questions, top_k=3.
#
# The original twelve questions were written alongside the chunker, in the
# document's own vocabulary, and scored 1.00. That was not retrieval quality; it
# was the questions being easy. Fifteen paraphrases -- the same questions as a
# grower would ask them -- took the overall number to 0.70 and the paraphrase half
# to 0.47. Those are the real figures.
#
# Before lowering anything, the obvious lever was measured. A larger embedder is
# NOT the answer here:
#
#   potion-base-8M        dim 256   all 0.70   paraphrase 0.47
#   potion-base-32M       dim 512   all 0.74   paraphrase 0.53
#   potion-retrieval-32M  dim 512   all 0.70   paraphrase 0.47
#
# Four times the model, twice the vector width, and a schema migration, for one
# extra question out of 27 -- and the retrieval-tuned variant buys nothing at all.
# ADR-003's rule applies: it does not earn its place. The bottleneck is the corpus
# and the chunking, not the embedder, and pretending otherwise would have cost a
# migration to hide the fact.
#
# Two floors rather than one, because a single average lets the easy half carry
# the hard half. Each sits one miss below what was measured.
RECALL_AT_3_FLOOR = 0.66            # 18/27
PARAPHRASE_RECALL_FLOOR = 0.40      # 6/15


@pytest.mark.db
def test_recall_at_3_over_the_labelled_questions(db_session):
    """Retrieval quality, gated -- because a claim without a gate rots.

    Ground truth is a chapter, not a chunk id (see the fixture's comment): chunk
    ids move whenever the corpus is regenerated, and a gate that goes red for
    that reason gets deleted rather than investigated.
    """
    from vinea.rag.store import ingest, search

    ingest(db_session, corpus.load_corpus(), source="test-recall")

    hit_count = 0
    misses: list[str] = []
    for question in QUESTIONS:
        results = search(db_session, question["query"], source="test-recall", top_k=3)
        found = {int(r.locator.split()[1].rstrip(":—-")) for r in results if r.locator.startswith("Chapter ")}
        if found & set(question["chapters"]):
            hit_count += 1
        else:
            misses.append(f"{question['id']}: wanted {question['chapters']}, got {sorted(found)}")

    recall = hit_count / len(QUESTIONS)
    assert recall >= RECALL_AT_3_FLOOR, (
        f"recall@3 = {recall:.2f} < {RECALL_AT_3_FLOOR} over {len(QUESTIONS)} questions.\n"
        + "\n".join(misses)
    )


@pytest.mark.db
def test_ingest_writes_no_vectors(db_session):
    """The property that keeps ADR-011 true rather than merely written down.

    ADR-008 built dense retrieval and justified it with recall@3 = 1.00 over
    twelve questions -- all of which were written alongside the chunker, in
    FAO-56's own vocabulary. Fifteen questions phrased the way a grower asks them
    produced the reversal:

        retriever      original 12   paraphrase 15   all 27
        hybrid            1.00           0.47          0.70
        dense only        0.92           0.53          0.70
        lexical only      0.92           0.67          0.78

    Lexical alone was *better*, not merely cheaper: the weak static embedder
    injected plausible-but-wrong passages that displaced correct lexical hits
    through the fusion. So the embedder, the model in the image and the vector
    query are gone.

    The columns remain, reserved, because ADR-008's revisit trigger still stands
    -- a corpus past ~10^5 chunks, or one spanning languages. This test is what
    stops them being quietly filled again without that measurement being redone.
    """
    from sqlalchemy import func, select

    from vinea.db.models import CorpusChunk
    from vinea.rag.store import ingest

    ingest(db_session, corpus.load_corpus()[:50], source="test-novec")
    embedded = db_session.execute(
        select(func.count())
        .select_from(CorpusChunk)
        .where(CorpusChunk.source == "test-novec", CorpusChunk.embedding.isnot(None))
    ).scalar_one()
    assert embedded == 0


@pytest.mark.db
def test_a_citation_survives_the_corpus_being_reingested(db_engine):
    """The record of what was cited outlives the index it points into.

    `corpus_chunks` is a cache: `TRUNCATE` is meant to be always safe, and
    re-chunking reassigns every id. Under the original `ON DELETE CASCADE` that
    deleted the whole citation row -- including the denormalised `locator`, the
    one field added precisely so a citation stays readable afterwards. The claim
    "TRUNCATE corpus_chunks is always safe" was false, and silently so.

    `SET NULL` keeps the row: the link to the passage is gone, the citation is
    not.
    """
    from sqlalchemy import text as sql
    from sqlmodel import Session

    from vinea.db.models import AdvisoryCitation
    from vinea.db.session import scope_to_ops
    from vinea.rag.store import ingest

    with Session(db_engine) as session:
        scope_to_ops(session)
        session.execute(sql("DELETE FROM advisories WHERE tenant = 'cite-test'"))
        advisory_id = session.execute(
            sql(
                "INSERT INTO advisories (tenant, run_date, target_date, irrigation, spray, "
                "reconciliation, deps_hash) VALUES ('cite-test', '2026-01-02', '2026-01-03', "
                "'{}', '{}', '{}', 'h') RETURNING id"
            )
        ).scalar_one()
        ingest(session, corpus.load_corpus()[:5], source="cite-test")
        chunk_id = session.execute(
            sql("SELECT id FROM corpus_chunks WHERE source = 'cite-test' ORDER BY id LIMIT 1")
        ).scalar_one()
        session.add(
            AdvisoryCitation(
                advisory_id=advisory_id, leg="irrigation", chunk_id=chunk_id,
                locator="Chapter 8 — ETc under soil water and salinity stress conditions", rank=1,
            )
        )
        session.commit()

        # Re-ingest: the cache is rebuilt and every id is reassigned.
        session.execute(sql("DELETE FROM corpus_chunks WHERE source = 'cite-test'"))
        session.commit()

        surviving = session.execute(
            sql("SELECT chunk_id, locator FROM advisory_citations WHERE advisory_id = :a"),
            {"a": advisory_id},
        ).all()
        session.execute(sql("DELETE FROM advisories WHERE tenant = 'cite-test'"))
        session.commit()

    assert len(surviving) == 1, "the citation was deleted with the cache it pointed into"
    assert surviving[0][0] is None                      # the link is gone
    assert surviving[0][1].startswith("Chapter 8")      # the citation is not


# --------------------------------------------------------------------------- #
# Queries are built from the night, not baked at import                       #
# --------------------------------------------------------------------------- #


RUN_DATE_FOR_QUERIES = date(2026, 7, 29)


def _irrigation_features(**overrides):
    """Tonight's real irrigation features, with named fields overridden.

    Built from the committed dataset rather than hand-constructed, so the
    branches are exercised against numbers the physics actually produces.
    """
    from vinea import config as _config
    from vinea.deps import WINE_GRAPES
    from vinea.features import build_features
    from vinea.ingest import load_weather

    data_dir = Path(_config.DEFAULT_DATA_DIR)
    hist, fc, dq = load_weather(
        sorted(data_dir.glob("*last-30d*.csv"))[-1],
        sorted(data_dir.glob("*next-7d*.csv"))[-1],
        date(2026, 7, 28),
    )
    features = build_features(hist, fc, dq, date(2026, 7, 28), WINE_GRAPES).irrigation
    return features.model_copy(update=overrides) if overrides else features


def test_the_query_changes_with_the_state_of_the_water_balance():
    """Retrieval has to be a function of the night, or it is a lookup table.

    Two module-level strings meant the same query every night for every tenant
    against a static corpus -- three passages that could have been pasted into
    the prompt at build time, with an entire pgvector pipeline behind them.
    """
    from vinea.rag.queries import irrigation_query

    stressed = irrigation_query(_irrigation_features())                       # past RAW
    comfortable = irrigation_query(_irrigation_features(current_depletion_mm=5.0))
    rainy = irrigation_query(
        _irrigation_features(current_depletion_mm=5.0, effective_rain_tomorrow_mm=12.0)
    )

    assert "water stress coefficient" in stressed
    assert "irrigation scheduling" in comfortable
    assert "effective rainfall" in rainy
    assert len({stressed, comfortable, rainy}) == 3


def test_the_query_is_deterministic():
    """Same features in, same query out.

    Retrieval must not add run-to-run variance on top of the model's own: an
    advisory that differs from yesterday's should differ because the weather did.
    """
    from vinea.contracts import SprayFeatures
    from vinea.rag.queries import irrigation_query, spray_query

    features = _irrigation_features()
    assert irrigation_query(features) == irrigation_query(features)

    spray = SprayFeatures(
        target_date=RUN_DATE_FOR_QUERIES, can_spray=True, limiting_factors=["wind"]
    )
    assert spray_query(spray) == spray_query(spray)


@pytest.mark.db
def test_different_nights_retrieve_different_passages(db_engine):
    """The claim, checked against the real index rather than against the string.

    Four water-balance states, four distinct passage sets. If this ever collapses
    to one set, the queries have stopped discriminating and the retrieval layer is
    back to being an expensive constant.
    """
    from vinea.rag.queries import irrigation_query

    states = {
        "past the trigger": _irrigation_features(),
        "comfortable": _irrigation_features(current_depletion_mm=5.0),
        "rain coming": _irrigation_features(
            current_depletion_mm=5.0, effective_rain_tomorrow_mm=12.0
        ),
        "missing et0": _irrigation_features(skipped_et0_hours=9),
    }
    retrieved = {
        label: tuple(p.chunk_id for p in retrieve.retrieve_for("irrigation", irrigation_query(f)))
        for label, f in states.items()
    }
    for label, chunks in retrieved.items():
        assert chunks, f"{label} retrieved nothing; is the corpus ingested?"
    assert len(set(retrieved.values())) > 1, (
        "every state retrieved the same passages -- the query is not discriminating: "
        f"{retrieved}"
    )


def test_not_every_branch_shifts_the_ranking_and_that_is_the_corpus_not_a_bug():
    """Measured: the spray query's wind branch moves the results; its rain branch
    does not.

    FAO-56 is an evapotranspiration manual, so it has a great deal to say about
    wind measurement height and comparatively little about spraying in the rain.
    A branch that does not shift the ranking is the corpus declining to answer,
    and the honest response is to record it rather than to keep adding synonyms
    until the numbers move.
    """
    from vinea.contracts import SprayFeatures
    from vinea.rag.queries import spray_query

    windy = SprayFeatures(
        target_date=RUN_DATE_FOR_QUERIES, can_spray=False,
        limiting_factors=["wind above 4.2 m/s all afternoon"],
    )
    rainy = SprayFeatures(
        target_date=RUN_DATE_FOR_QUERIES, can_spray=False,
        limiting_factors=["rain within the rain-fast window"],
    )
    assert "wind speed measurement" in spray_query(windy)
    assert "precipitation and wetting events" in spray_query(rainy)


@pytest.mark.db
def test_recall_on_the_paraphrased_half_has_its_own_floor(db_session):
    """The hard half, gated separately so the easy half cannot carry it.

    A single averaged number lets twelve questions written in the manual's own
    vocabulary hide fifteen written the way a grower speaks. Measured: 0.47 on
    the paraphrases against 0.93 on the originals -- the gap is the honest
    statement of how good this retrieval actually is on natural language.
    """
    from vinea.rag.store import ingest, search

    ingest(db_session, corpus.load_corpus(), source="test-para")

    paraphrases = [q for q in QUESTIONS if q.get("style") == "paraphrase"]
    assert paraphrases, "the paraphrase set has gone missing from the fixture"

    hits, misses = 0, []
    for question in paraphrases:
        results = search(db_session, question["query"], source="test-para", top_k=3)
        found = {int(r.locator.split()[1].rstrip(":—-")) for r in results if r.locator.startswith("Chapter ")}
        if found & set(question["chapters"]):
            hits += 1
        else:
            misses.append(f"{question['id']}: wanted {question['chapters']}, got {sorted(found)}")

    recall = hits / len(paraphrases)
    assert recall >= PARAPHRASE_RECALL_FLOOR, (
        f"paraphrase recall@3 = {recall:.2f} < {PARAPHRASE_RECALL_FLOOR} "
        f"over {len(paraphrases)} questions.\n" + "\n".join(misses)
    )
