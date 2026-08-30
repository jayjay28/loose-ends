"""§v3 workstream 4 — how a phone finds this engine.

The engine knows every door the phone could use and hands the full list over
at pairing; `/transport` keeps the list fresh afterwards. Bonjour is the
zero-typing path and is pinned only at the module seam — the network itself
is not a thing a unit test should touch.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from lifeline.api import auth, transport
from lifeline.api.app import app


def local_client() -> TestClient:
    return TestClient(app, client=("127.0.0.1", 51000))


def remote_client() -> TestClient:
    return TestClient(app, client=("192.168.1.50", 51000))


# ------------------------------------------------------------------- doors
def test_every_door_lan_first(monkeypatch):
    monkeypatch.setattr(transport, "lan_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(transport, "tailnet_url", lambda: "https://mac.tailnet.ts.net")
    monkeypatch.setenv("LIFELINE_PORT", "8100")
    assert transport.urls() == ["http://192.168.1.20:8100",
                                "https://mac.tailnet.ts.net"]
    assert transport.reachable_url() == "http://192.168.1.20:8100"


def test_no_network_still_answers_something(monkeypatch):
    monkeypatch.setattr(transport, "lan_ip", lambda: None)
    monkeypatch.setattr(transport, "tailnet_url", lambda: None)
    monkeypatch.delenv("LIFELINE_PORT", raising=False)
    assert transport.urls() == []
    assert transport.reachable_url() == "http://localhost:8000"


def test_a_bad_port_env_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("LIFELINE_PORT", "eight thousand")
    assert transport.port() == 8000


# ------------------------------------------------------------------ routes
def test_pairing_hands_over_every_door(monkeypatch):
    monkeypatch.setattr(transport, "urls",
                        lambda: ["http://192.168.1.20:8000",
                                 "https://mac.tailnet.ts.net"])
    code = auth.start_pairing()["code"]
    body = local_client().post("/pair/claim", json={"code": code}).json()
    assert body["urls"] == ["http://192.168.1.20:8000",
                            "https://mac.tailnet.ts.net"]
    assert body["token"].startswith("le_")


def test_transport_reports_the_current_doors(monkeypatch):
    monkeypatch.setattr(transport, "urls", lambda: ["http://192.168.1.20:8000"])
    body = local_client().get("/transport").json()
    assert body["urls"] == ["http://192.168.1.20:8000"]
    assert body["service_type"] == "_loose-ends._tcp"


def test_transport_is_behind_the_gate():
    assert remote_client().get("/transport").status_code == 401


def test_a_paired_phone_can_refresh_its_doors(monkeypatch):
    monkeypatch.setattr(transport, "urls", lambda: ["http://192.168.1.20:8000"])
    code = auth.start_pairing()["code"]
    token = local_client().post("/pair/claim", json={"code": code}).json()["token"]
    response = remote_client().get("/transport",
                                   headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["urls"] == ["http://192.168.1.20:8000"]


# ----------------------------------------------------------------- bonjour
def test_advertising_respects_the_kill_switch(monkeypatch):
    monkeypatch.setenv("LIFELINE_NO_BONJOUR", "1")
    assert transport.advertise() is False


def test_advertising_without_a_lan_address_declines(monkeypatch):
    monkeypatch.delenv("LIFELINE_NO_BONJOUR", raising=False)
    monkeypatch.setattr(transport, "lan_ip", lambda: None)
    assert transport.advertise() is False
