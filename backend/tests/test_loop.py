"""The agentic loop (§v1.4): tool dispatch, conclusion, budget cap, provenance,
and the /ask port. Providers are faked — no network."""
from __future__ import annotations

import json
import types

from fastapi.testclient import TestClient

from lifeline import db
from lifeline.api.app import app
from lifeline.assistant import loop, registry
from lifeline.extraction import providers

client = TestClient(app)


def fake_provider(script):
    """A provider whose complete_with_tools pops canned turns off `script`."""
    turns = list(script)

    def complete_with_tools(messages, *, tools, system=None, max_tokens=1024):
        return turns.pop(0) if turns else {"text": "ran out of script", "tool_calls": []}

    mod = types.SimpleNamespace(__name__="fake", complete_with_tools=complete_with_tools)
    return mod


def use(monkeypatch, provider):
    monkeypatch.setattr(providers, "available", lambda: [provider])


def echo_tool(marker="ok"):
    return registry.Tool(
        name="echo",
        description="echo test tool",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        fn=lambda q="": {"echo": q, "marker": marker},
    )


def test_loop_calls_tool_then_concludes(monkeypatch):
    use(
        monkeypatch,
        fake_provider(
            [
                {"text": "", "tool_calls": [{"id": "c1", "name": "echo", "input": {"q": "hi"}}]},
                {"text": "The echo said hi.", "tool_calls": []},
            ]
        ),
    )
    run = loop.run_loop("test goal", trigger="ask", tools=[echo_tool()])
    assert run is not None
    assert run.conclusion == "The echo said hi."
    assert run.iterations == 2
    assert run.tool_calls[0]["name"] == "echo"
    assert json.loads(run.tool_calls[0]["result"])["echo"] == "hi"

    # Provenance persisted.
    row = db.get_connection().execute(
        "SELECT trigger, goal, status, iterations FROM loop_runs WHERE id = ?", (run.run_id,)
    ).fetchone()
    assert tuple(row) == ("ask", "test goal", "concluded", 2)


def test_unknown_tool_is_survivable(monkeypatch):
    use(
        monkeypatch,
        fake_provider(
            [
                {"text": "", "tool_calls": [{"id": "c1", "name": "nope", "input": {}}]},
                {"text": "done anyway", "tool_calls": []},
            ]
        ),
    )
    run = loop.run_loop("g", trigger="ask", tools=[echo_tool()])
    assert run.conclusion == "done anyway"
    assert json.loads(run.tool_calls[0]["result"])["error"].startswith("unknown tool")


def test_iteration_cap_forces_conclusion(monkeypatch):
    call = {"text": "", "tool_calls": [{"id": "c", "name": "echo", "input": {"q": "again"}}]}
    use(monkeypatch, fake_provider([call, call, {"text": "forced wrap-up", "tool_calls": []}]))
    run = loop.run_loop("g", trigger="sweep", tools=[echo_tool()], max_iterations=2)
    assert run.conclusion == "forced wrap-up"
    assert run.iterations == 2
    assert len(run.tool_calls) == 2


def test_no_tool_capable_provider_returns_none(monkeypatch):
    # A provider without complete_with_tools (e.g. only complete_json) is skipped.
    bare = types.SimpleNamespace(__name__="bare")
    monkeypatch.setattr(providers, "available", lambda: [bare])
    assert loop.run_loop("g", trigger="ask", tools=[echo_tool()]) is None


def test_failing_tool_reports_error_not_crash(monkeypatch):
    bad = registry.Tool(
        name="bad",
        description="always raises",
        input_schema={"type": "object", "properties": {}},
        fn=lambda: 1 / 0,
    )
    use(
        monkeypatch,
        fake_provider(
            [
                {"text": "", "tool_calls": [{"id": "c1", "name": "bad", "input": {}}]},
                {"text": "handled", "tool_calls": []},
            ]
        ),
    )
    run = loop.run_loop("g", trigger="ask", tools=[bad])
    assert run.conclusion == "handled"
    assert "error" in json.loads(run.tool_calls[0]["result"])


def test_ask_rides_the_loop(monkeypatch):
    use(
        monkeypatch,
        fake_provider(
            [
                {
                    "text": "",
                    "tool_calls": [
                        {"id": "c1", "name": "search_messages", "input": {"query": "dentist"}}
                    ],
                },
                {"text": "You have no dentist messages.", "tool_calls": []},
            ]
        ),
    )
    resp = client.post("/ask", json={"question": "when is my dentist appointment?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "You have no dentist messages."
    assert body["sources"] == ["search_messages"]


def test_ask_falls_back_without_loop(monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: [])
    resp = client.post("/ask", json={"question": "what do I owe Maya?"})
    assert resp.status_code == 200
    assert resp.json()["answer"]  # heuristic fallback still answers


def test_falling_back_is_recorded_as_degradation_not_health(monkeypatch):
    """A working fallback is how a dead primary key goes unnoticed.

    Before this, success cleared `llm:last_error` unconditionally — so once the
    Gemini fallback worked, a rejected Anthropic key looked exactly like a
    healthy day in the only place `/health` reads.
    """
    dead = types.SimpleNamespace(
        __name__="lifeline.extraction.claude",
        complete_with_tools=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("credit balance too low")),
    )
    alive = fake_provider([{"text": "found it", "tool_calls": []}])
    alive.__name__ = "lifeline.extraction.gemini"
    monkeypatch.setattr(providers, "available", lambda: [dead, alive])

    result = loop.run_loop("what changed?", trigger="worker", tools=[])

    assert result is not None
    note = db.get_sync_state("llm:last_error")
    assert "degraded" in note and "gemini" in note and "credit balance too low" in note


def test_the_first_provider_working_clears_the_error(monkeypatch):
    db.set_sync_state("llm:last_error", "something old")
    use(monkeypatch, fake_provider([{"text": "fine", "tool_calls": []}]))

    assert loop.run_loop("what changed?", trigger="worker", tools=[]) is not None
    assert db.get_sync_state("llm:last_error") == ""


def test_a_narrated_search_is_sent_back_even_after_tool_calls(monkeypatch):
    """§v2.9 regression, seen on the first live Ask card: the model read the
    grounding block ("already known: enrolled in LPS preschool"), called a
    tool, then concluded "to find where it's located, I would need to search
    your messages". Announcing a search you have the iterations left to run
    is not an answer — unlike an honest miss, it gets handed back once."""
    use(
        monkeypatch,
        fake_provider(
            [
                {"text": "", "tool_calls": [{"id": "t1", "name": "echo", "input": {"q": "Lia"}}]},
                {"text": "Lia is enrolled in LPS preschool, but the address is "
                         "not stored. To find where it's located, I would need "
                         "to search your messages.", "tool_calls": []},
                {"text": "Brightwood Pre-School, 412 Alder Ave — from the Meet and "
                         "Greet invite.", "tool_calls": []},
            ]
        ),
    )
    run = loop.run_loop("Where is Lia's daycare?", trigger="ask", tools=[echo_tool()])
    assert "Brightwood" in run.conclusion, "the nudge produced a real answer"
    assert run.iterations == 3, "one nudge, then the earned conclusion"
