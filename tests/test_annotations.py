"""The annotation flow: the judgement the oracle cannot make, given a door.

`annotations` has existed since the eval work and nothing ever wrote to it -- the
table was the design and the door was missing. The oracle can prove the numbers
are right; whether the *advice* was right needs an agronomist, and these tests
cover the path their judgement takes: API in, table row, API out, isolation held.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from tests.conftest import open_ops_session
from vinea import keys
from vinea.api import main
from vinea.db.models import Annotation

pytestmark = pytest.mark.db

TENANT = "acme"
RUN_DATE = date(2025, 2, 8)


@pytest.fixture
def client(committing_db):
    global _KEY, _OTHER
    with open_ops_session(committing_db) as session:
        _KEY = keys.issue(session, tenant=TENANT, label="annot test").secret
        _OTHER = keys.issue(session, tenant="olivares", label="annot other").secret
        session.commit()
    main.app.dependency_overrides[main.get_engine] = lambda: committing_db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _seed(engine, tenant=TENANT):
    from tests.test_api import _seed_advisory

    _seed_advisory(engine, tenant=tenant)


def _post(client, body, *, key=None, tenant=TENANT):
    return client.post(
        f"/advisories/{tenant}/{RUN_DATE}/annotations",
        json=body,
        headers={"X-API-Key": key or _KEY},
    )


AGRONOMIST_DISAGREES = {
    "reviewer_role": "agronomist",
    "reviewer_id": "maria",
    "verdict": "disagree",
    "leg": "irrigation",
    "comment": "133 mm in one application will run straight past the root zone.",
}


def test_a_judgement_round_trips(client, committing_db):
    """POST writes the row, GET reads it back, and the shape survives."""
    _seed(committing_db)
    r = _post(client, AGRONOMIST_DISAGREES)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["verdict"] == "disagree"
    assert body["promoted_to_golden"] is False, "promotion is a curation step, never automatic"

    listed = client.get(
        f"/advisories/{TENANT}/{RUN_DATE}/annotations", headers={"X-API-Key": _KEY}
    ).json()
    assert [a["reviewer_id"] for a in listed] == ["maria"]
    assert listed[0]["comment"].startswith("133 mm")


def test_the_row_lands_on_the_right_advisory(client, committing_db):
    _seed(committing_db)
    _post(client, AGRONOMIST_DISAGREES)
    with open_ops_session(committing_db) as session:
        row = session.exec(select(Annotation)).one()
        from vinea.db.models import Advisory

        advisory = session.get(Advisory, row.advisory_id)
        assert advisory.tenant == TENANT
        assert advisory.run_date == RUN_DATE


def test_annotating_a_missing_advisory_is_404_not_an_orphan(client, committing_db):
    """Feedback about an advisory that was never produced is feedback about nothing."""
    r = _post(client, AGRONOMIST_DISAGREES)  # nothing seeded
    assert r.status_code == 404
    with open_ops_session(committing_db) as session:
        assert session.exec(select(Annotation)).all() == []


def test_another_tenants_key_cannot_annotate(client, committing_db):
    """403 before the advisory is even looked up -- the path check, as everywhere."""
    _seed(committing_db)
    r = _post(client, AGRONOMIST_DISAGREES, key=_OTHER)
    assert r.status_code == 403
    with open_ops_session(committing_db) as session:
        assert session.exec(select(Annotation)).all() == []


def test_the_closed_sets_are_closed(client, committing_db):
    """A typo'd role must fail at the edge, not become a category nothing handles.

    The same rule the ENUM enforces one layer down; rejecting it at the schema
    turns a 500 (enum coercion error) into a 422 that names the field.
    """
    _seed(committing_db)
    for bad in (
        {**AGRONOMIST_DISAGREES, "reviewer_role": "agronimist"},
        {**AGRONOMIST_DISAGREES, "verdict": "maybe"},
        {**AGRONOMIST_DISAGREES, "leg": "weather"},
        {**AGRONOMIST_DISAGREES, "reviewer_id": ""},
    ):
        assert _post(client, bad).status_code == 422, bad


def test_whole_advisory_feedback_carries_no_leg(client, committing_db):
    """NULL leg means "about the advisory as a whole" -- the farmer's normal case."""
    _seed(committing_db)
    r = _post(
        client,
        {
            "reviewer_role": "farmer",
            "reviewer_id": "kostas",
            "verdict": "unclear",
            "comment": "Which of the two windows do I actually use?",
        },
    )
    assert r.status_code == 201
    assert r.json()["leg"] is None


def test_disagreement_is_not_consensus(client, committing_db):
    """Both roles speak, they disagree, and both rows survive verbatim.

    The `reviewer_role` column exists because an agronomist judging correctness
    and a farmer judging clarity will disagree, and the disagreement is the
    signal. Anything that collapsed the two -- last-write-wins, one row per
    advisory -- would throw away the field's reason for existing.
    """
    _seed(committing_db)
    _post(client, AGRONOMIST_DISAGREES)
    _post(
        client,
        {
            "reviewer_role": "farmer",
            "reviewer_id": "kostas",
            "verdict": "agree",
            "comment": "Clear enough for me.",
        },
    )
    listed = client.get(
        f"/advisories/{TENANT}/{RUN_DATE}/annotations", headers={"X-API-Key": _KEY}
    ).json()
    assert [(a["reviewer_role"], a["verdict"]) for a in listed] == [
        ("agronomist", "disagree"),
        ("farmer", "agree"),
    ]


def test_an_annotated_advisory_with_no_annotations_is_an_empty_list(client, committing_db):
    """Silence is a normal state, not an error."""
    _seed(committing_db)
    r = client.get(f"/advisories/{TENANT}/{RUN_DATE}/annotations", headers={"X-API-Key": _KEY})
    assert r.status_code == 200
    assert r.json() == []
