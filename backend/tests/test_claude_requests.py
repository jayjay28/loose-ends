"""The shape of what we actually send Anthropic.

These exist because of a silent, long-running failure: every Claude call in
`extraction/claude.py` ended its message list with an assistant prefill
(`{"role": "assistant", "content": "{"}`). That is rejected with a 400 on
Claude Opus 4.6 and later — including `claude-opus-5`, the configured default
extraction model — so extraction, follow-up linking, and every `complete_json`
caller had been failing and falling through to Gemini or the heuristic.
`providers.run` and `link_followup` both swallow exceptions, so nothing said so.

A prefill is one line to re-add and reads like an optimisation, which is
exactly why it needs a test rather than a comment.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lifeline.config import Config, set_config
from lifeline.extraction import claude


class _Block(SimpleNamespace):
    pass


class _FakeMessages:
    """Records the kwargs of every request and returns canned JSON."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[_Block(type="text", text=self.reply)])


@pytest.fixture
def fake_client(monkeypatch):
    def install(reply: str):
        messages = _FakeMessages(reply)
        monkeypatch.setattr(claude, "_client", lambda: SimpleNamespace(messages=messages))
        return messages

    # A key must look configured for the call sites that read config.
    set_config(Config(anthropic_api_key="test-key", extraction_model="claude-opus-5"))
    return install


def _roles(call) -> list:
    return [m["role"] for m in call["messages"]]


def test_classify_batch_sends_no_assistant_prefill(fake_client):
    messages = fake_client('{"items": [{"type": "promise"}]}')
    result = claude.classify_batch(
        [{"id": "m1", "person": "Tess", "timestamp": "2026-08-01T00:00:00Z",
          "source": "imessage", "text": "call the vet"}]
    )
    assert result == {"items": [{"type": "promise"}], "entities": []}
    assert _roles(messages.calls[0]) == ["user"]


def test_link_followup_sends_no_prefill_and_constrains_the_shape(fake_client):
    messages = fake_client('{"links_to_item_id": "abc"}')
    assert claude.link_followup({"raw_text": "x"}, [{"id": "abc"}]) == "abc"
    call = messages.calls[0]
    assert _roles(call) == ["user"]
    # Structured outputs are the supported replacement for the prefill.
    schema = call["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["links_to_item_id"]


def test_complete_json_sends_no_prefill(fake_client):
    messages = fake_client('{"answer": "yes"}')
    assert claude.complete_json("a question", system="Return JSON only") == '{"answer": "yes"}'
    assert _roles(messages.calls[0]) == ["user"]
    assert "output_config" not in messages.calls[0]


def test_complete_json_passes_a_schema_through_as_structured_output(fake_client):
    messages = fake_client('{"threads": []}')
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    claude.complete_json("cluster these", schema=schema)
    assert messages.calls[0]["output_config"] == {
        "format": {"type": "json_schema", "schema": schema}
    }


def test_complete_json_recovers_json_wrapped_in_prose(fake_client):
    """Without a prefill the model may add a sentence or a fence; `_parse_json`
    is what makes dropping the prefill safe for callers with no schema."""
    fake_client('Here you go:\n```json\n{"title": "Puerto Rico trip"}\n```')
    text = claude.complete_json("name this")
    assert claude._parse_json(text) == {"title": "Puerto Rico trip"}


def test_both_providers_accept_the_same_complete_json_signature():
    """`providers.run` calls one lambda against whichever provider is up, so a
    kwarg only Claude understands would break the Gemini fallback."""
    import inspect

    from lifeline.extraction import gemini

    assert (
        set(inspect.signature(claude.complete_json).parameters)
        == set(inspect.signature(gemini.complete_json).parameters)
    )
