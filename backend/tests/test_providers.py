"""The LLM provider chain: Claude -> Gemini -> heuristic fallback signal."""
from __future__ import annotations

from lifeline.extraction import budget, providers


class _Boom:
    __name__ = "boom"

    def call(self):
        raise RuntimeError("credit balance too low")


class _Good:
    __name__ = "good"

    def call(self):
        return "ok"


def test_falls_through_to_the_next_provider(monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: [_Boom(), _Good()])
    monkeypatch.setattr(budget, "allow", lambda: True)
    charged = []
    monkeypatch.setattr(budget, "record", lambda: charged.append(1))

    assert providers.run(lambda p: p.call(), "x") == "ok"
    assert charged == [1]  # budget charged once, only on the successful call


def test_returns_none_when_every_provider_fails(monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: [_Boom(), _Boom()])
    monkeypatch.setattr(budget, "allow", lambda: True)
    monkeypatch.setattr(budget, "record", lambda: None)

    assert providers.run(lambda p: p.call(), "x") is None


def test_returns_none_when_no_provider_is_configured(monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: [])
    assert providers.run(lambda p: "unused", "x") is None


def test_stops_when_budget_is_spent(monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: [_Good()])
    monkeypatch.setattr(budget, "allow", lambda: False)
    assert providers.run(lambda p: p.call(), "x") is None
