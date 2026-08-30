"""The relay's promises: stateless auth, content-free payloads, bounded abuse."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def relay(monkeypatch):
    monkeypatch.setenv("RELAY_SIGNING_KEY", "test-signing-key")
    import app as relay_app
    importlib.reload(relay_app)          # fresh in-memory rate limits per test
    return relay_app


@pytest.fixture()
def client(relay):
    return TestClient(relay.app)


@pytest.fixture()
def sent(relay, monkeypatch):
    """Capture what would have gone to Apple."""
    calls = []

    def fake(tokens, payload, priority, collapse_id):
        calls.append({"tokens": tokens, "payload": payload,
                      "priority": priority, "collapse_id": collapse_id})
        return {t: "ok" for t in tokens}

    monkeypatch.setattr(relay, "_apns_send", fake)
    return calls


def creds(client) -> dict:
    body = client.post("/v1/register").json()
    return {"Authorization": f"Bearer {body['install_id']}.{body['install_secret']}"}


TOKEN = "ab" * 32


def test_registration_needs_no_database(client, relay):
    """The secret is recomputable from the id — that's the whole design."""
    body = client.post("/v1/register").json()
    assert body["install_secret"] == relay._secret_for(body["install_id"])


def test_a_push_carries_the_knock_and_never_the_message(client, sent):
    r = client.post("/v1/push", headers=creds(client), json={
        "device_tokens": [TOKEN], "level": "active",
        "thread_id": "th-1", "finding_id": "f-9",
    })
    assert r.status_code == 200 and r.json()["results"][TOKEN] == "ok"

    payload = sent[0]["payload"]
    assert payload["aps"]["alert"]["body"] == "Something moved on an end you're carrying."
    assert payload["aps"]["mutable-content"] == 1, "the phone replaces the words"
    assert payload["thread_id"] == "th-1" and payload["finding_id"] == "f-9"
    # ... and there is no field an engine could have smuggled words through.
    assert set(payload) == {"aps", "relay", "thread_id", "finding_id"}


def test_forged_and_missing_credentials_bounce(client):
    body = {"device_tokens": [TOKEN]}
    assert client.post("/v1/push", json=body).status_code == 401
    assert client.post("/v1/push", json=body,
                       headers={"Authorization": "Bearer nope.wrong"}).status_code == 401


def test_denylisted_installs_are_out(client, monkeypatch):
    headers = creds(client)
    install_id = headers["Authorization"][7:].split(".")[0]
    monkeypatch.setenv("RELAY_DENYLIST", f"other,{install_id}")
    assert client.post("/v1/push", headers=headers,
                       json={"device_tokens": [TOKEN]}).status_code == 401


def test_garbage_tokens_and_levels_are_refused(client, sent):
    headers = creds(client)
    assert client.post("/v1/push", headers=headers,
                       json={"device_tokens": ["not-hex"]}).status_code == 400
    assert client.post("/v1/push", headers=headers,
                       json={"device_tokens": [TOKEN], "level": "screaming"}).status_code == 400
    assert sent == []


def test_the_install_rate_limit_holds(client, sent, relay):
    headers = creds(client)
    for _ in range(relay.PER_INSTALL_PER_HOUR):
        assert client.post("/v1/push", headers=headers,
                           json={"device_tokens": [TOKEN]}).status_code in (200, 429)
    r = client.post("/v1/push", headers=headers, json={"device_tokens": [TOKEN]})
    assert r.status_code == 429


def test_passive_pushes_ride_low_priority(client, sent):
    client.post("/v1/push", headers=creds(client),
                json={"device_tokens": [TOKEN], "level": "passive"})
    assert sent[0]["priority"] == "5"
    assert "sound" not in sent[0]["payload"]["aps"]
