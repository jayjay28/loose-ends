"""§v3 workstream 5 — the engine's side of the push relay.

The developer's machine has the APNs key and pushes words directly. Every
other engine knocks through the relay with ids only, and the phone fetches
the words back from the engine via /push/card. These tests pin the fork and
the fetch.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lifeline import db
from lifeline.api.app import app
from lifeline.notifications import apns, relay, scheduler
from lifeline.models import InterruptionLevel


@pytest.fixture()
def a_device():
    db.register_device("ab" * 32)
    return "ab" * 32


def _queue_finding_push():
    from lifeline import threads

    thread = threads.create(title="pajama order for Nora")
    finding = db.save_finding(threads.make_finding(
        thread.id, kind="finding", headline="It shipped Tuesday.", importance=0.9))
    notification_id = db.queue_notification(
        "finding", InterruptionLevel.TIME_SENSITIVE,
        thread.title, finding.headline, finding_id=finding.id)
    return thread, finding, notification_id


def test_without_a_key_the_knock_goes_through_the_relay(monkeypatch, a_device):
    thread, finding, _ = _queue_finding_push()

    knocks = []
    monkeypatch.setattr(relay, "configured", lambda: True)
    monkeypatch.setattr(relay, "send",
                        lambda tokens, **kw: knocks.append((tokens, kw)) or {})
    direct = []
    monkeypatch.setattr(apns, "send", lambda *a, **kw: direct.append(a) or {})

    scheduler.flush()

    assert direct == [], "no key, no direct push"
    (tokens, kw), = knocks
    assert tokens == [a_device]
    assert kw["finding_id"] == finding.id and kw["thread_id"] == thread.id
    assert kw["level"] == "time-sensitive"
    assert "title" not in kw and "body" not in kw, "ids only — never words"


def test_with_a_key_the_words_go_direct_and_the_relay_stays_out(monkeypatch, a_device):
    _queue_finding_push()

    monkeypatch.setenv("APNS_KEY_PATH", "/tmp/k.p8")
    monkeypatch.setenv("APNS_KEY_ID", "K")
    monkeypatch.setenv("APNS_TEAM_ID", "T")
    # get_config caches; drop the cached instance so has_apns sees the env.
    from lifeline import config
    monkeypatch.setattr(config, "_config", None)

    knocks = []
    monkeypatch.setattr(relay, "configured", lambda: True)
    monkeypatch.setattr(relay, "send", lambda *a, **kw: knocks.append(a) or {})
    direct = []
    monkeypatch.setattr(apns, "send",
                        lambda tokens, payload, collapse_id=None:
                        direct.append(payload) or {})

    scheduler.flush()

    assert knocks == [], "the developer's machine never knocks"
    assert direct and direct[0]["aps"]["alert"]["body"] == "It shipped Tuesday."


def test_the_phone_fetches_the_words_from_its_own_engine():
    thread, finding, _ = _queue_finding_push()
    client = TestClient(app)

    card = client.get("/push/card", params={"finding_id": finding.id}).json()
    assert card["title"] == "pajama order for Nora"
    assert card["body"] == "It shipped Tuesday."
    assert card["thread_id"] == thread.id, "the tap knows which door"

    assert client.get("/push/card", params={"finding_id": "nope"}).status_code == 404


def test_the_card_falls_back_to_the_thread_when_only_that_id_arrived():
    thread, finding, _ = _queue_finding_push()
    client = TestClient(app)
    card = client.get("/push/card", params={"thread_id": thread.id}).json()
    assert card["title"] == thread.title
    assert card["body"] == "It shipped Tuesday."
