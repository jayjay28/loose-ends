"""§v2.9 — Ask as answer cards, not chat.

An answer is a card: the answer, the receipts it rests on (openable by id),
the facts the world model contributed (correctable by id), and the trace.
These tests pin the card's contract end to end through the HTTP surface.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lifeline import db, world
from lifeline.api.app import app
from lifeline.assistant import loop as assistant_loop

from tests.conftest import make_conversation, make_message, make_person


@pytest.fixture
def client():
    return TestClient(app)


class FakeRun:
    def __init__(self, conclusion, tool_calls):
        self.conclusion = conclusion
        self.tool_calls = tool_calls


def test_an_answer_carries_its_receipts_and_what_was_known(client, monkeypatch):
    make_person("lia", "Lia Carter", relationship=None)
    world.record_fact("lia", "attends", "Brightwood Pre-School", confidence=0.9)
    make_conversation("gmail:t1", source="gmail", name="Nia")
    message = make_message("Meet and Greet at Brightwood Pre-School Monday",
                           conversation_id="gmail:t1", person_id=None,
                           metadata={"subject": "Meet and Greet"},
                           external_id="g-meet", source="gmail")

    def fake_loop(prompt, **kwargs):
        assert "Brightwood Pre-School" in prompt, "grounding fed the prompt"
        return FakeRun(
            "Lia's daycare is Brightwood Pre-School.",
            [{"name": "search_mail",
              "input": {"query": "Brightwood"},
              "result": json.dumps([{
                  "message_id": message.id, "source": "gmail",
                  "subject": "Meet and Greet", "timestamp": message.timestamp,
              }])}],
        )

    monkeypatch.setattr(assistant_loop, "run_loop", fake_loop)
    body = client.post("/ask", json={"question": "Where is Lia's daycare?"}).json()

    assert body["answer"] == "Lia's daycare is Brightwood Pre-School."
    assert body["receipts"][0]["ref_id"] == message.id, "openable receipt"
    assert "Meet and Greet" in body["receipts"][0]["label"]
    assert body["knew"][0]["entity"] == "Lia Carter"
    assert body["knew"][0]["value"] == "Brightwood Pre-School"
    assert body["knew"][0]["fact_id"], "correctable by id"
    assert body["trace"], "the trace rides along"

    # ... and the card persists: the Ask surface is a reference.
    history = client.get("/asks").json()
    assert history[0]["question"] == "Where is Lia's daycare?"
    assert history[0]["receipts"][0]["ref_id"] == message.id


def test_the_wrong_door_supersedes_and_the_correction_wins(client, monkeypatch):
    """The mockup's sheet: forget retires a fact; correct retires it and the
    user's word lands at full confidence. Nothing is deleted."""
    make_person("alex", "Alex Carter", relationship=None)
    wrong = world.record_fact("alex", "relation_to_user", "child or dependent",
                              confidence=0.7)

    r = client.post(f"/world/facts/{wrong.id}", json={"action": "correct",
                                                      "value": "this is the user"})
    assert r.json()["status"] == "corrected"

    active = world.facts_for("alex")
    assert [f.value for f in active] == ["this is the user"]
    assert active[0].confidence == 1.0, "the user's word, at full strength"
    history = world.facts_for("alex", include_superseded=True)
    assert len(history) == 2, "the wrong claim stays inspectable"

    # forget on the corrected fact retires it too
    r = client.post(f"/world/facts/{active[0].id}", json={"action": "forget"})
    assert r.json()["status"] == "superseded"
    assert world.facts_for("alex") == []

    assert client.post("/world/facts/nope", json={"action": "forget"}).status_code == 404
    assert client.post(f"/world/facts/{wrong.id}", json={"action": "sudo"}).status_code == 400


def test_receipts_prefer_the_message_opened_in_full(client, monkeypatch):
    """A get_message call is a stronger receipt than a passing search hit —
    it lands first, and duplicates collapse onto one chip."""
    make_conversation("imessage:t1", name="Nia")
    make_person("nia", "Nia", relationship="partner")
    m1 = make_message("first", person_id="nia", external_id="im-1")
    m2 = make_message("second", person_id="nia", external_id="im-2")

    def fake_loop(prompt, **kwargs):
        return FakeRun("Answered.", [
            {"name": "search_messages", "input": {}, "result": json.dumps([
                {"message_id": m1.id, "source": "imessage", "text": "first",
                 "timestamp": m1.timestamp},
                {"message_id": m2.id, "source": "imessage", "text": "second",
                 "timestamp": m2.timestamp},
            ])},
            {"name": "get_message", "input": {}, "result": json.dumps(
                {"message_id": m1.id, "source": "imessage", "text": "first",
                 "timestamp": m1.timestamp})},
        ])

    monkeypatch.setattr(assistant_loop, "run_loop", fake_loop)
    body = client.post("/ask", json={"question": "anything new?"}).json()
    refs = [r["ref_id"] for r in body["receipts"]]
    assert refs[0] == m1.id, "the opened message leads"
    assert refs.count(m1.id) == 1, "no duplicate chips"
    assert m2.id in refs


# ------------------------------------------------- audit F1 + F3 regressions

def test_read_attachment_returns_the_document_and_honest_errors():
    from lifeline.assistant import tools as atools
    from lifeline.models import Attachment

    make_conversation("gmail:t1", source="gmail", name="LPS")
    message = make_message("FAQ attached", conversation_id="gmail:t1",
                           person_id=None, external_id="g-faq", source="gmail")
    db.insert_attachment(Attachment(
        message_id=message.id, source="gmail", filename="LPS FAQ.pdf",
        mime="application/pdf", sha256="sha-faq",
        text="Eligibility: children must be 3 or 4 by October 1, 2026 and "
             "residents of Lakeview Township." + "x" * 7000,
        parsed_at="2026-08-29T00:00:00+00:00",
    ))
    db.insert_attachment(Attachment(
        message_id=message.id, source="gmail", filename="scan.pdf",
        mime="application/pdf", sha256="sha-scan",
        error="no extractable text (scanned?)",
        parsed_at="2026-08-29T00:00:00+00:00",
    ))

    result = atools.read_attachment(message.id)
    by_name = {a["filename"]: a for a in result["attachments"]}
    assert "October 1, 2026" in by_name["LPS FAQ.pdf"]["text"]
    assert by_name["LPS FAQ.pdf"]["truncated"] is True, "long files page"
    assert by_name["scan.pdf"]["error"] == "no extractable text (scanned?)", \
        "the honest answer for a scan, verbatim"

    paged = atools.read_attachment(message.id, filename="LPS FAQ.pdf", offset=6000)
    assert paged["attachments"][0]["offset"] == 6000

    assert "error" in atools.read_attachment("no-such-message")


def test_search_hits_through_documents_carry_the_documents_words():
    from lifeline.assistant import tools as atools
    from lifeline.models import Attachment

    make_conversation("gmail:t1", source="gmail", name="LPS")
    message = make_message("see the attached FAQ", conversation_id="gmail:t1",
                           person_id=None, external_id="g-faq2", source="gmail")
    db.insert_attachment(Attachment(
        message_id=message.id, source="gmail", filename="FAQ.pdf",
        mime="application/pdf", sha256="sha-faq2",
        text="Enrollment eligibility requires residency in Lakeview Township.",
        parsed_at="2026-08-29T00:00:00+00:00",
    ))

    hits = atools.search_messages(query="eligibility residency")
    assert hits and hits[0]["via_attachment"] == "FAQ.pdf"
    assert "residency in Lakeview Township" in hits[0]["attachment_excerpt"], \
        "the document's words, not the covering email's"


def test_large_tool_results_no_longer_lose_their_receipts(client, monkeypatch):
    """Audit F3: results were truncated to 2000 chars in the call log, the
    receipts extractor's json.loads failed mid-object, and 52% of answers
    shipped bare. The log now keeps the full result in memory."""
    from lifeline.assistant import loop as aloop
    from lifeline.assistant import registry as areg
    from lifeline.extraction import providers as prov

    make_conversation("imessage:t1", name="Nia")
    make_person("nia", "Nia", relationship="partner")
    big_messages = [make_message(f"filler message number {n} with plenty of text "
                                 + "y" * 120, person_id="nia",
                                 external_id=f"im-big-{n}") for n in range(12)]

    def fake_provider(messages, *, tools, system=None, max_tokens=1024):
        if len([m for m in messages if m["role"] == "tool"]) == 0:
            return {"text": "", "tool_calls": [{"id": "c1", "name": "search_messages",
                                                "input": {"query": "filler plenty", "limit": 12}}]}
        return {"text": "Found them.", "tool_calls": []}

    import types
    provider = types.SimpleNamespace(__name__="fake", complete_with_tools=fake_provider)
    monkeypatch.setattr(prov, "available", lambda: [provider])

    run = aloop.run_loop("find the filler", trigger="ask",
                         tools=list(areg.READ_TOOLS), max_iterations=3)
    import json as _j
    result_blob = run.tool_calls[0]["result"]
    assert len(result_blob) > 2000, "big enough to have been truncated before"
    _j.loads(result_blob)                       # parses whole — the fix
