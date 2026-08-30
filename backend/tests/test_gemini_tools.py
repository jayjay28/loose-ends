"""Gemini's tool translation, and the fallback it used to break.

The worker offers web tools on every pass. Those are Anthropic *server* tools —
`{"type": "web_search_20250305", "name": "web_search"}`, no `input_schema`,
because Anthropic runs them rather than us. Gemini's converter indexed
`input_schema` unconditionally, so the moment Claude stopped answering, the
fallback raised `KeyError: 'input_schema'` on its first turn and the worker
recorded `worked: 0` every cycle with nothing anywhere saying why.
"""
from __future__ import annotations

import copy

from lifeline.assistant import registry
from lifeline.extraction import gemini


def _declarations(tools):
    for t in tools:
        if "functionDeclarations" in t:
            return t["functionDeclarations"]
    return []


def _natives(tools):
    return [t for t in tools if "functionDeclarations" not in t]


def test_server_tool_does_not_raise():
    """The regression itself: a schemaless tool used to be a KeyError."""
    out = gemini._to_gemini_tools([registry.ServerTool(name="web_search",
                                                       type="web_search_20250305").schema])
    assert _natives(out) == [{"googleSearch": {}}]


def test_web_tools_map_to_gemini_natives():
    out = gemini._to_gemini_tools([
        registry.ServerTool(name="web_search", type="web_search_20260209").schema,
        registry.ServerTool(name="web_fetch", type="web_fetch_20260209").schema,
    ])
    assert _natives(out) == [{"googleSearch": {}}, {"urlContext": {}}]


def test_dated_variants_collapse_to_one_native():
    """Both web_search revisions are the same capability to Gemini; declaring
    it twice is a 400."""
    out = gemini._to_gemini_tools([
        registry.ServerTool(name="web_search", type="web_search_20250305").schema,
        registry.ServerTool(name="web_search", type="web_search_20260209").schema,
    ])
    assert _natives(out) == [{"googleSearch": {}}]


def test_unknown_server_tool_is_dropped_not_raised():
    out = gemini._to_gemini_tools([{"type": "code_execution_20250522", "name": "code_execution"}])
    assert out == []


def test_normal_tool_becomes_a_function_declaration():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    out = gemini._to_gemini_tools([{"name": "echo", "description": "d", "input_schema": schema}])
    assert _declarations(out) == [{"name": "echo", "description": "d", "parameters": schema}]


def test_no_argument_tool_declares_no_parameters():
    """An empty `properties` is a 400 on Gemini ("should be non-empty for
    OBJECT type") where Claude takes it happily."""
    out = gemini._to_gemini_tools([
        {"name": "list_watchers", "description": "d", "input_schema": {"type": "object", "properties": {}}},
    ])
    assert _declarations(out) == [{"name": "list_watchers", "description": "d"}]


def test_the_worker_real_tool_list_translates():
    """The list that actually broke it, end to end."""
    tools = list(registry.READ_TOOLS) + registry.web_tools("claude-haiku-4-5-20251001")
    out = gemini._to_gemini_tools([t.schema for t in tools])
    assert _natives(out) == [{"googleSearch": {}}]
    assert len(_declarations(out)) == len(registry.READ_TOOLS)
    assert all("parameters" not in d or d["parameters"]["properties"] for d in _declarations(out))


# --------------------------------------------------------------- the retry


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


_OK = {"candidates": [{"content": {"parts": [{"text": "done"}]}}]}


def _capture(monkeypatch, responses):
    """Record each request body; return the canned responses in order."""
    sent = []
    queue = list(responses)

    def post(url, headers=None, json=None, timeout=None):
        # Deep-copied because the retry mutates the same body dict in place —
        # httpx serialises at call time, so that is safe there and only a trap
        # for a capture that holds the reference.
        sent.append(copy.deepcopy(json))
        return queue.pop(0)

    monkeypatch.setattr(gemini.httpx, "post", post)
    return sent


def test_retries_without_natives_when_the_model_rejects_the_mix(monkeypatch):
    sent = _capture(monkeypatch, [
        _Response(400, text="Tool use with function calling is unsupported"),
        _Response(200, _OK),
    ])
    tools = [
        {"name": "echo", "description": "d", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        registry.ServerTool(name="web_search", type="web_search_20250305").schema,
    ]

    result = gemini.complete_with_tools([{"role": "user", "content": "hi"}], tools=tools)

    assert result["text"] == "done"
    assert _natives(sent[0]["tools"]) == [{"googleSearch": {}}]   # tried with web
    assert _natives(sent[1]["tools"]) == []                        # retried without
    assert _declarations(sent[1]["tools"])                         # functions kept


def test_a_400_with_no_natives_to_drop_still_raises(monkeypatch):
    _capture(monkeypatch, [_Response(400, text="bad model name")])
    tools = [{"name": "echo", "description": "d",
              "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}]

    try:
        gemini.complete_with_tools([{"role": "user", "content": "hi"}], tools=tools)
    except gemini.ClassifierError as exc:
        assert "bad model name" in str(exc)
    else:
        raise AssertionError("expected ClassifierError")


def test_a_429_does_not_buy_a_second_doomed_call(monkeypatch):
    """Depleted credits is how both keys on this machine actually failed. It is
    not a tool-compatibility problem, so there is nothing to drop and retry."""
    sent = _capture(monkeypatch, [_Response(429, text="prepayment credits are depleted")])
    tools = [
        {"name": "echo", "description": "d", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        registry.ServerTool(name="web_search", type="web_search_20250305").schema,
    ]

    try:
        gemini.complete_with_tools([{"role": "user", "content": "hi"}], tools=tools)
    except gemini.ClassifierError as exc:
        assert "depleted" in str(exc)
    else:
        raise AssertionError("expected ClassifierError")
    assert len(sent) == 1
