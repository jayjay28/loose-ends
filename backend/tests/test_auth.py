"""§v3 workstream 3 — the API stops being open.

A request is either from this Mac, or it carries a token a pairing minted.
These tests pin the gate's trust rules and the whole life of a pairing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from lifeline import db
from lifeline.api import auth
from lifeline.api.app import app


def local_client() -> TestClient:
    """Genuinely this machine: loopback peer, nothing forwarded."""
    return TestClient(app, client=("127.0.0.1", 51000))


def remote_client() -> TestClient:
    """The world — a LAN neighbour's address. (TestClient's default peer,
    "testclient", is deliberately trusted by the gate, so the untrusted
    case has to say who it is.)"""
    return TestClient(app, client=("192.168.1.50", 51000))


def test_the_machine_talking_to_itself_needs_no_token():
    assert local_client().get("/health").status_code != 401


def test_a_stranger_gets_the_door():
    response = remote_client().get("/threads")
    assert response.status_code == 401
    assert "pair this device" in response.json()["detail"]


def test_a_proxied_request_does_not_inherit_the_macs_trust():
    """Tailscale serve connects from loopback but stamps X-Forwarded-For on
    everything it relays — the difference between the Mac talking to itself
    and the world arriving through a door."""
    response = local_client().get("/threads", headers={"X-Forwarded-For": "100.64.0.7"})
    assert response.status_code == 401


def test_pairing_mints_a_token_that_opens_the_door():
    code = auth.start_pairing()["code"]

    claim = remote_client().post("/pair/claim",
                                 json={"code": code, "device_name": "test iPhone"})
    assert claim.status_code == 200
    token = claim.json()["token"]

    assert remote_client().get(
        "/threads", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 200

    # ... and the proxied path opens too — the token is the trust, not the route.
    assert local_client().get(
        "/threads",
        headers={"X-Forwarded-For": "100.64.0.7", "Authorization": f"Bearer {token}"},
    ).status_code == 200


def test_a_code_spends_exactly_once():
    code = auth.start_pairing()["code"]
    assert remote_client().post("/pair/claim", json={"code": code}).status_code == 200
    assert remote_client().post("/pair/claim", json={"code": code}).status_code == 404


def test_an_expired_code_is_worthless():
    code = auth.start_pairing()["code"]
    stale = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    db.get_connection().execute(
        "UPDATE pairing_codes SET expires_at = ? WHERE code = ?", (stale, code))
    db.get_connection().commit()
    assert remote_client().post("/pair/claim", json={"code": code}).status_code == 404


def test_claims_are_forgiving_about_case_and_whitespace():
    code = auth.start_pairing()["code"]
    sloppy = f"  {code.lower()}  "
    assert remote_client().post("/pair/claim", json={"code": sloppy}).status_code == 200


def test_a_revoked_token_stops_working():
    minted = auth.mint_token("old phone")
    header = {"Authorization": f"Bearer {minted['token']}"}
    assert remote_client().get("/threads", headers=header).status_code == 200

    assert local_client().post(f"/auth/tokens/{minted['token_id']}/revoke").status_code == 200
    assert remote_client().get("/threads", headers=header).status_code == 401


def test_garbage_bearers_do_not_crash_the_gate():
    for bearer in ("", "le_", "nope", "le_zz", "le_deadbeef_wrongsecret"):
        response = remote_client().get(
            "/threads", headers={"Authorization": f"Bearer {bearer}"})
        assert response.status_code == 401


def test_the_wizard_can_watch_a_pairing_land():
    code = auth.start_pairing()["code"]
    assert local_client().get("/pair/status", params={"code": code}).json()["claimed"] is False
    remote_client().post("/pair/claim", json={"code": code})
    assert local_client().get("/pair/status", params={"code": code}).json()["claimed"] is True


