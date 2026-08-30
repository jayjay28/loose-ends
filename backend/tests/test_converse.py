"""/converse (§v1.5): one door — the loop decides tell vs ask, and the trace
carries the receipts (honest misses included)."""
from __future__ import annotations

import types

from fastapi.testclient import TestClient

from lifeline import db
from lifeline.api.app import app
from lifeline.extraction import providers

client = TestClient(app)


def fake_provider(script):
    turns = list(script)

    def complete_with_tools(messages, *, tools, system=None, max_tokens=1024):
        return turns.pop(0) if turns else {"text": "out of script", "tool_calls": []}

    return types.SimpleNamespace(__name__="fake", complete_with_tools=complete_with_tools)


def test_ask_returns_answer_with_trace(monkeypatch):
    monkeypatch.setattr(
        providers, "available",
        lambda: [fake_provider([
            {"text": "", "tool_calls": [
                {"id": "c1", "name": "find_person", "input": {"name": "Katie"}},
                {"id": "c2", "name": "search_messages", "input": {"query": "availability"}},
            ]},
            {"text": "One matter, sir: Katie awaits your availability.", "tool_calls": []},
        ])],
    )
    body = client.post("/converse", json={"text": "what do I owe Katie?"}).json()
    assert body["reply"].startswith("One matter")
    assert body["facts"] == []
    tools_fired = [t["tool"] for t in body["trace"]]
    assert tools_fired == ["find_person", "search_messages"]
    # find_person on an unknown name is an honest miss, not silence.
    assert body["trace"][0]["ok"] is False
    assert "nothing found" in body["trace"][0]["summary"]
    assert "Katie" in body["trace"][0]["summary"]


def test_tell_records_facts_and_traces_the_save(monkeypatch):
    monkeypatch.setattr(
        providers, "available",
        lambda: [fake_provider([
            {"text": "", "tool_calls": [
                {"id": "c1", "name": "record_fact",
                 "input": {"statement": "wants the Meridian job", "subject_type": "self"}},
            ]},
            {"text": "Duly noted, sir.", "tool_calls": []},
        ])],
    )
    body = client.post("/converse", json={"text": "I want that Meridian job"}).json()
    assert body["reply"] == "Duly noted, sir."
    assert [f["statement"] for f in body["facts"]] == ["wants the Meridian job"]
    assert body["trace"][0]["tool"] == "record_fact"
    assert body["trace"][0]["ok"] is True
    assert "saved" in body["trace"][0]["summary"]
    assert db.list_facts(subject_type="self")


def test_offline_statement_kept_verbatim(monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: [])
    body = client.post("/converse", json={"text": "I'm moving in September"}).json()
    assert body["reply"] == "Noted."
    assert body["facts"][0]["statement"] == "I'm moving in September"
    assert body["trace"] == []


def test_offline_question_gets_tool_report(monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: [])
    body = client.post("/converse", json={"text": "what do I owe Maya?"}).json()
    assert body["reply"]                      # heuristic fallback answers
    assert body["facts"] == []                # a question records nothing


def test_empty_400():
    assert client.post("/converse", json={"text": " "}).status_code == 400


def test_offline_mixed_input_keeps_the_statement(monkeypatch):
    """QA round 2, defect 2: 'I moved my gym to mornings. What do I owe Maya?'
    must not drop the statement half on the fallback path."""
    monkeypatch.setattr(providers, "available", lambda: [])
    body = client.post(
        "/converse",
        json={"text": "I moved my gym sessions to mornings. Also what do I owe Maya?"},
    ).json()
    assert [f["statement"] for f in body["facts"]] == ["I moved my gym sessions to mornings."]
    assert body["reply"].startswith("Noted.")          # both halves acknowledged
    assert db.list_facts(subject_type="self")


def test_failed_provider_surfaces_in_health(monkeypatch):
    """QA round 2, defect 1: a configured-but-dead key must not degrade
    silently — the loop records the error where /health shows it."""
    import types

    def boom(messages, *, tools, system=None, max_tokens=1024):
        raise RuntimeError("credit balance is too low")

    dead = types.SimpleNamespace(__name__="claude", complete_with_tools=boom)
    monkeypatch.setattr(providers, "available", lambda: [dead])

    body = client.post("/converse", json={"text": "what do I owe Maya?"}).json()
    assert body["reply"]                               # fallback still answers
    health = client.get("/health").json()
    assert "credit balance" in (health["llm_last_error"] or "")

    # A later success clears it.
    ok = types.SimpleNamespace(
        __name__="claude",
        complete_with_tools=lambda messages, *, tools, system=None, max_tokens=1024: {
            "text": "All well.", "tool_calls": []
        },
    )
    monkeypatch.setattr(providers, "available", lambda: [ok])
    client.post("/converse", json={"text": "what do I owe Maya?"})
    assert client.get("/health").json()["llm_last_error"] is None


def test_conversation_memory_resolves_pronouns(monkeypatch):
    """The screenshot bug: 'compose a follow up to her' after 'who is Katie?'
    must arrive with the prior turns as context, not as turn zero."""
    seen: list = []

    def complete_with_tools(messages, *, tools, system=None, max_tokens=1024):
        seen.append([(m["role"], m.get("content")) for m in messages])
        return {"text": "Katie Marsh is a recruiter.", "tool_calls": []}

    monkeypatch.setattr(
        providers, "available",
        lambda: [types.SimpleNamespace(__name__="fake", complete_with_tools=complete_with_tools)],
    )

    first = client.post("/converse", json={"text": "Who is Katie?"}).json()
    session = first["session_id"]
    assert session

    seen.clear()
    client.post("/converse", json={"text": "compose a follow up to her", "session_id": session})

    # The second call carries the first exchange before the new question.
    contents = [c for _, c in seen[0]]
    assert "Who is Katie?" in contents
    assert "Katie Marsh is a recruiter." in contents
    assert contents[-1] == "compose a follow up to her"


def test_new_session_starts_clean(monkeypatch):
    def complete_with_tools(messages, *, tools, system=None, max_tokens=1024):
        complete_with_tools.last = messages
        return {"text": "ok", "tool_calls": []}

    monkeypatch.setattr(
        providers, "available",
        lambda: [types.SimpleNamespace(__name__="fake", complete_with_tools=complete_with_tools)],
    )
    client.post("/converse", json={"text": "first thing"})
    client.post("/converse", json={"text": "unrelated thing"})   # no session_id
    assert len(complete_with_tools.last) == 1                    # turn zero, as asked


def test_transcript_endpoint_returns_turns(monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: [])
    body = client.post("/converse", json={"text": "QA note about the garage"}).json()
    session = body["session_id"]

    turns = client.get(f"/converse/{session}").json()["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["text"] == "QA note about the garage"
    assert turns[1]["text"] == "Noted."
    assert turns[1]["facts"][0]["statement"] == "QA note about the garage"


def test_unknown_session_is_empty_not_404():
    body = client.get("/converse/nope").json()
    assert body["turns"] == []
