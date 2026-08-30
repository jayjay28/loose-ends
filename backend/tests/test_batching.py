"""Phase D — staging several owed items to one person and clearing them with a
single reply (POST /threads/{id}/draft, POST /items/batch/done)."""
from __future__ import annotations

from conftest import make_item, make_person, make_conversation
from fastapi.testclient import TestClient

from lifeline import db
from lifeline.api.app import app

client = TestClient(app)


def _kiba_with_two_open():
    make_conversation()
    make_person("kiba", "Kiba", "friend", handles=["+15551234567"])
    i1 = make_item(person_id="kiba", person="Kiba", item_type="question",
                   text="what time to run tomorrow?")
    i1.suggested_reply = "around 7am"
    db.save_item(i1)
    i2 = make_item(person_id="kiba", person="Kiba", item_type="question",
                   text="any thoughts on my resume?")
    i2.suggested_reply = "looks strong, tighten the summary"
    db.save_item(i2)
    return i1, i2


def test_draft_folds_replies_and_resolves_the_handle():
    i1, i2 = _kiba_with_two_open()
    r = client.post("/conversations/kiba/draft", json={"item_ids": [i1.id, i2.id]})
    assert r.status_code == 200
    body = r.json()
    assert body["handle"] == "+15551234567"
    assert set(body["item_ids"]) == {i1.id, i2.id}
    # No LLM key in the test env, so the heuristic stitch runs: it must fold the
    # user's own drafted replies (and never leak imperative action text).
    assert "7am" in body["reply"]
    assert "summary" in body["reply"]
    assert "Handle" not in body["reply"]


def test_draft_ignores_other_peoples_and_closed_items():
    i1, i2 = _kiba_with_two_open()
    make_person("dev", "Dev", "friend")
    foreign = make_item(person_id="dev", person="Dev", text="not kiba's")
    i2.status = "completed"
    db.save_item(i2)
    r = client.post("/conversations/kiba/draft", json={"item_ids": [i1.id, i2.id, foreign.id]})
    assert r.status_code == 200
    assert r.json()["item_ids"] == [i1.id]     # only kiba's still-open item


def test_draft_404_when_nothing_is_open():
    make_person("ghost", "Ghost", "friend")
    r = client.post("/conversations/ghost/draft", json={"item_ids": ["nope"]})
    assert r.status_code == 404


def test_batch_done_closes_every_covered_item():
    i1, i2 = _kiba_with_two_open()
    r = client.post("/items/batch/done", json={"item_ids": [i1.id, i2.id]})
    assert r.status_code == 200
    assert set(r.json()["completed"]) == {i1.id, i2.id}
    assert db.get_item(i1.id).status == "completed"
    assert db.get_item(i2.id).status == "completed"
