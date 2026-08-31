"""§v3 workstream 2 — the setup wizard's engine room.

The gcloud and Settings choreography is a human affair; what these tests pin
is everything the wizard *claims*: credentials land in ~/.lifeline/env and
take effect without a restart, a downloaded client secret is adopted on
sight and moved out of Downloads, and every status light is a live check.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lifeline import config as config_mod
from lifeline.api import wizard
from lifeline.api.app import app


@pytest.fixture()
def home(tmp_path, monkeypatch):
    env_file = tmp_path / "lifeline" / "env"
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(config_mod, "ENV_FILE", env_file)
    monkeypatch.setattr(wizard, "DOWNLOADS", downloads)
    monkeypatch.setattr(config_mod, "_config", None)
    return tmp_path


def test_credentials_land_in_the_env_file_and_take_effect_now(home, monkeypatch):
    monkeypatch.delenv("WIZ_TEST_KEY", raising=False)
    wizard.write_env({"WIZ_TEST_KEY": "abc"})

    text = config_mod.ENV_FILE.read_text()
    assert "WIZ_TEST_KEY=abc" in text
    assert os.environ["WIZ_TEST_KEY"] == "abc", "adopted without a restart"
    assert oct(config_mod.ENV_FILE.stat().st_mode)[-3:] == "600", "credentials file is private"

    # A second write keeps earlier keys — the file is a ledger, not a scratchpad.
    wizard.write_env({"OTHER": "1"})
    assert "WIZ_TEST_KEY=abc" in config_mod.ENV_FILE.read_text()
    monkeypatch.delenv("WIZ_TEST_KEY", raising=False)
    monkeypatch.delenv("OTHER", raising=False)


def test_the_env_file_is_a_floor_never_a_ceiling(home, monkeypatch):
    config_mod.ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    config_mod.ENV_FILE.write_text("WIZ_FLOOR=file\n")
    monkeypatch.setenv("WIZ_FLOOR", "shell")
    config_mod.load_env_file(config_mod.ENV_FILE)
    assert os.environ["WIZ_FLOOR"] == "shell", "the process environment wins"
    monkeypatch.delenv("WIZ_FLOOR")


def test_a_downloaded_client_secret_is_adopted_on_sight(home, monkeypatch):
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    blob = {"installed": {"client_id": "abc.apps.googleusercontent.com",
                          "client_secret": "s3cret"}}
    dropped = wizard.DOWNLOADS / "client_secret_abc.apps.googleusercontent.com.json"
    dropped.write_text(json.dumps(blob))

    result = wizard.ingest_client_secret()

    assert result == {"client_id": "abc.apps.googleusercontent.com"}
    assert os.environ["GOOGLE_CLIENT_ID"] == "abc.apps.googleusercontent.com"
    assert not dropped.exists(), "a secret does not live in Downloads"
    assert (config_mod.ENV_FILE.parent / dropped.name).exists(), "moved, not lost"
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)


def test_garbage_downloads_are_ignored(home):
    (wizard.DOWNLOADS / "client_secret_junk.json").write_text("not json")
    (wizard.DOWNLOADS / "client_secret_empty.json").write_text("{}")
    assert wizard.ingest_client_secret() is None


def test_the_status_board_is_live_checks(home, monkeypatch):
    monkeypatch.setattr(wizard, "_fda_readable", lambda: True)
    monkeypatch.setattr(wizard, "_gcloud", lambda: "/opt/gcloud")
    monkeypatch.setattr(wizard, "_gcloud_account", lambda: "you@gmail.com")
    monkeypatch.setattr(wizard, "_reachable_url", lambda: "https://mac.tailnet.ts.net")

    board = wizard.status()
    assert board["fda"]["readable"] is True
    assert board["google"]["account"] == "you@gmail.com"
    assert board["pairing"]["reach_url"] == "https://mac.tailnet.ts.net"
    assert isinstance(board["pairing"]["devices"], int)


def test_a_malformed_key_never_leaves_the_machine(home, monkeypatch):
    called = []
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: called.append(a))
    verdict = wizard.save_key("not-a-key")
    assert verdict["ok"] is False and called == [], "rejected before any network"


def test_a_working_key_is_adopted_after_a_real_answer(home, monkeypatch):
    import httpx

    class FakeResponse:
        status_code = 200

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    verdict = wizard.save_key("sk-ant-" + "x" * 40)
    assert verdict["ok"] is True
    assert os.environ["ANTHROPIC_API_KEY"].startswith("sk-ant-")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_the_pair_endpoint_hands_back_a_scannable_code(home, monkeypatch):
    monkeypatch.setattr(wizard, "_reachable_url", lambda: "http://192.168.1.9:8000")
    client = TestClient(app, client=("127.0.0.1", 51000))
    body = client.post("/setup/pair").json()
    assert len(body["code"]) == 8
    assert body["qr_svg"].lstrip().startswith("<?xml") or "<svg" in body["qr_svg"]
    assert body["reach_url"] == "http://192.168.1.9:8000"


def test_the_setup_page_is_loopback_only():
    local = TestClient(app, client=("127.0.0.1", 51000))
    assert local.get("/setup").status_code == 200
    stranger = TestClient(app, client=("192.168.1.66", 51000))
    assert stranger.get("/setup").status_code == 401, "the wizard is for the person at the Mac"


# ------------------------------------------- field reports, 2026-08-31
def test_gcloud_is_found_off_the_launchd_path(monkeypatch):
    """Field report: "I installed gcloud and it keeps saying to install it."
    Under launchd the PATH is the bare system one, so `which` can't see a
    Homebrew or home-directory SDK — the known homes are searched too."""
    monkeypatch.setattr(wizard.shutil, "which", lambda _: None)
    monkeypatch.setattr(wizard.os, "access",
                        lambda p, _: p == "/opt/homebrew/bin/gcloud")
    assert wizard._gcloud() == "/opt/homebrew/bin/gcloud"

    monkeypatch.setattr(wizard.os, "access", lambda p, _: False)
    assert wizard._gcloud() is None


def test_a_grant_the_process_cannot_see_reports_pending_restart(monkeypatch):
    """Field report: "I added zsh to FDA but it didn't update." TCC caches
    the denial against the running process; a fresh child sees the grant,
    and the board says a restart is what's missing."""
    monkeypatch.setattr(wizard, "_fda_readable", lambda: False)
    monkeypatch.setattr(
        wizard.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0))
    fda = wizard._fda_probe()
    assert fda == {"readable": False, "granted_pending_restart": True}


def test_no_grant_at_all_is_just_waiting(monkeypatch):
    monkeypatch.setattr(wizard, "_fda_readable", lambda: False)
    monkeypatch.setattr(
        wizard.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1))
    fda = wizard._fda_probe()
    assert fda == {"readable": False, "granted_pending_restart": False}


def test_an_unmanaged_engine_never_exits_itself(monkeypatch):
    """The self-restart belongs to launchd-managed installs only — exiting a
    dev uvicorn would just be dying."""
    monkeypatch.delenv("LIFELINE_MANAGED", raising=False)
    monkeypatch.setattr(wizard, "_restart_scheduled", False)
    fired = []
    monkeypatch.setattr(wizard.threading, "Timer",
                        lambda *a, **k: fired.append(a) or type("T", (), {"start": lambda s: None})())
    wizard._restart_to_adopt_grant()
    assert fired == []
