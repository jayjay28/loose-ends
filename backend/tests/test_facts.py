"""Facts store + POST /tell (§v1.4 pillar B): loop-driven extraction, offline
verbatim fallback, and the db round-trip."""
from __future__ import annotations

import types

from fastapi.testclient import TestClient

from lifeline import db
from lifeline.api.app import app
from lifeline.extraction import providers
from lifeline.models import Fact

client = TestClient(app)


def test_fact_roundtrip_and_soft_delete():
    fact = db.upsert_fact(
        Fact(subject_type="person", subject_id="katie-marsh", statement="recruiter at Meridian")
    )
    got = db.get_fact(fact.id)
    assert got.statement == "recruiter at Meridian"
    assert db.list_facts(subject_type="person", subject_id="katie-marsh")

    got.status = "dismissed"
    db.upsert_fact(got)
    assert db.list_facts(subject_type="person", subject_id="katie-marsh") == []
    assert db.list_facts(subject_type="person", subject_id="katie-marsh", include_dismissed=True)


def test_tell_records_facts_via_loop(monkeypatch):
    """The loop calls record_fact per fact; /tell echoes them back."""
    turns = [
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "name": "record_fact",
                    "input": {
                        "statement": "wants the Meridian Labs job",
                        "subject_type": "self",
                    },
                },
                {
                    "id": "c2",
                    "name": "record_fact",
                    "input": {
                        "statement": "is a recruiter at Meridian Labs",
                        "subject_type": "person",
                        "subject_id": "katie-marsh",
                    },
                },
            ],
        },
        {"text": "Got it — noted the job interest and Katie's role.", "tool_calls": []},
    ]

    def complete_with_tools(messages, *, tools, system=None, max_tokens=1024):
        return turns.pop(0) if turns else {"text": "done", "tool_calls": []}

    fake = types.SimpleNamespace(__name__="fake", complete_with_tools=complete_with_tools)
    monkeypatch.setattr(providers, "available", lambda: [fake])

    resp = client.post("/tell", json={"text": "I want that job at Meridian; Katie is their recruiter"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"].startswith("Got it")
    statements = {f["statement"] for f in body["facts"]}
    assert statements == {"wants the Meridian Labs job", "is a recruiter at Meridian Labs"}
    assert all(f["source"] == "user" for f in body["facts"])

    # Persisted, subject-scoped.
    assert db.list_facts(subject_type="person", subject_id="katie-marsh")


def test_tell_offline_saves_verbatim(monkeypatch):
    """No provider → the user's words are never dropped."""
    monkeypatch.setattr(providers, "available", lambda: [])
    resp = client.post("/tell", json={"text": "I'm moving apartments in September"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "Noted."
    assert len(body["facts"]) == 1
    assert body["facts"][0]["statement"] == "I'm moving apartments in September"
    assert db.list_facts(subject_type="self")


def test_tell_empty_400():
    assert client.post("/tell", json={"text": "  "}).status_code == 400


def test_model_of_you_groups_and_edits(monkeypatch):
    from tests.conftest import make_person

    make_person("katie-marsh", "Katie Marsh", relationship=None)
    db.upsert_fact(Fact(subject_type="self", statement="moving in September"))
    kf = db.upsert_fact(
        Fact(subject_type="person", subject_id="katie-marsh", statement="recruiter at Meridian")
    )
    db.upsert_fact(Fact(subject_type="topic", subject_id="meridian-job", statement="FDE role, wants it"))

    body = client.get("/model").json()
    assert [f["statement"] for f in body["you"]] == ["moving in September"]
    assert body["people"][0]["name"] == "Katie Marsh"
    assert body["people"][0]["facts"][0]["statement"] == "recruiter at Meridian"
    assert body["topics"][0]["name"] == "meridian-job"

    # Edit becomes authoritative.
    edited = client.patch(f"/facts/{kf.id}", json={"statement": "recruiter at Meridian Labs"}).json()
    assert edited["statement"] == "recruiter at Meridian Labs"
    assert edited["source"] == "user" and edited["confidence"] == 1.0

    # Delete is soft.
    client.delete(f"/facts/{kf.id}")
    assert db.get_fact(kf.id).status == "dismissed"
    assert client.get("/model").json()["people"] == []


def test_fact_edit_404():
    assert client.patch("/facts/nope", json={"statement": "x"}).status_code == 404
