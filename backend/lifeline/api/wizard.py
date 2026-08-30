"""§v3 workstream 2 — the setup wizard's engine room.

The wizard lives by three rules (the signed wireframes state them):

  * **It advances itself.** Every wait names what it's watching — a Google
    sign-in, a downloaded file, a Settings grant — and the status poll flips
    forward the moment the thing happens. No "Next" button that might lie.
  * **Say who does each minute.** Steps are marked ours or the user's.
  * **Verify by doing.** "Messages readable" means chat.db actually opened;
    "connected" means a real API answered. Every ✓ is a receipt.

This module is the doing: live checks, the gcloud automation, the Downloads
watcher that ingests `client_secret_*.json` on sight, and the one place
credentials get written — `~/.lifeline/env`, never a shell rc file. The
routes in `app.py` are thin wrappers; the page is `setup_page.html`.

Google can't be driven end to end: there is no public API for creating an
OAuth client, so the wizard automates everything around that gap (sign-in,
project, API enables, deep links, the download watch) and hands the user a
short, pointed set of clicks for the middle.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import db
from .. import config as config_mod
from ..config import get_config
from ..ingestion import google_auth

log = logging.getLogger(__name__)

PROJECT_KEY = "wizard:google_project"
DOWNLOADS = Path.home() / "Downloads"

# The gcloud automation journals here; /setup/status reads it. In-memory on
# purpose — a restart mid-setup just re-runs idempotent steps.
_google_run: Dict[str, Any] = {"running": False, "log": [], "error": None}
_google_lock = threading.Lock()


# ----------------------------------------------------------- the env floor
def write_env(values: Dict[str, str]) -> None:
    """Write credentials into ~/.lifeline/env and adopt them immediately —
    the running engine must not need the restart we forgot elsewhere."""
    path = config_mod.ENV_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: Dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()
    existing.update(values)
    lines = ["# Loose Ends engine credentials — written by the setup wizard.",
             "# The process environment wins over this file; edit freely."]
    lines += [f"{k}={v}" for k, v in sorted(existing.items())]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    for key, value in values.items():
        os.environ[key] = value          # adopt now, file is for next boot
    config_mod._config = None            # drop the cached Config


# ------------------------------------------------------------- the checks
def _fda_readable() -> bool:
    """Verify by doing: open the live Messages database."""
    chat = Path.home() / "Library" / "Messages" / "chat.db"
    try:
        with open(chat, "rb") as handle:
            handle.read(16)
        return True
    except OSError:
        return False


def _gcloud() -> Optional[str]:
    return shutil.which("gcloud")


def _gcloud_account() -> Optional[str]:
    if not _gcloud():
        return None
    try:
        out = subprocess.run(
            ["gcloud", "auth", "list", "--filter=status:ACTIVE",
             "--format=value(account)"],
            capture_output=True, text=True, timeout=15)
        account = out.stdout.strip().splitlines()
        return account[0] if account else None
    except (subprocess.SubprocessError, OSError):
        return None


def _reachable_url() -> str:
    """Where the phone should point. §v3 ws4 moved the knowledge of the
    engine's doors into `transport`; this stays as the wizard's one call."""
    from . import transport

    return transport.reachable_url()


def status() -> Dict[str, Any]:
    """The whole board, computed live. Every ✓ here is a receipt."""
    cfg = get_config()
    with _google_lock:
        run = dict(_google_run, log=list(_google_run["log"]))
    client_ready = bool(cfg.google_client_id and cfg.google_client_secret)
    connected = False
    if client_ready:
        try:
            connected = google_auth.is_connected()
        except Exception:
            connected = False
    return {
        "google": {
            "gcloud_installed": bool(_gcloud()),
            "account": _gcloud_account(),
            "project": db.get_sync_state(PROJECT_KEY),
            "automation": run,
            "client_ready": client_ready,
            "connected": connected,
        },
        "fda": {"readable": _fda_readable()},
        "key": {"present": bool(cfg.anthropic_api_key)},
        "pairing": {
            "devices": len(db.list_devices()),
            "reach_url": _reachable_url(),
        },
    }


# ------------------------------------------------- the gcloud automation
def _journal(line: str) -> None:
    with _google_lock:
        _google_run["log"].append(line)
    log.info("wizard/google: %s", line)


def _run(args: List[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def start_google_automation() -> Dict[str, Any]:
    """Sign in, create the project, switch on the two APIs. Everything
    Google allows through a terminal, without the user; idempotent, so
    running it twice converges instead of failing."""
    with _google_lock:
        if _google_run["running"]:
            return {"started": False, "reason": "already running"}
        _google_run.update({"running": True, "error": None, "log": []})

    def work() -> None:
        try:
            if not _gcloud():
                raise RuntimeError(
                    "gcloud isn't installed — run: brew install google-cloud-sdk")

            if not _gcloud_account():
                _journal("Opening the Google sign-in tab — finish it there…")
                result = _run(["gcloud", "auth", "login", "--quiet"], timeout=600)
                if result.returncode != 0:
                    raise RuntimeError(f"sign-in failed: {result.stderr[-300:]}")
            _journal(f"Signed into Google as {_gcloud_account()}")

            project = db.get_sync_state(PROJECT_KEY)
            if not project:
                project = f"loose-ends-{secrets.token_hex(3)}"
                _journal(f"Creating your own private project ({project})…")
                result = _run(["gcloud", "projects", "create", project,
                               "--name=Loose Ends"])
                if result.returncode != 0:
                    raise RuntimeError(f"project creation failed: {result.stderr[-300:]}")
                db.set_sync_state(PROJECT_KEY, project)
            _journal("Project ready — you are the only user it will ever have")

            _journal("Switching on Gmail access…")
            _journal("Switching on Calendar access…")
            result = _run(["gcloud", "services", "enable",
                           "gmail.googleapis.com", "calendar-json.googleapis.com",
                           f"--project={project}"], timeout=300)
            if result.returncode != 0:
                raise RuntimeError(f"enabling APIs failed: {result.stderr[-300:]}")
            _journal("Both APIs on — the 403 that haunts fresh projects can't happen here")
            _journal("done")
        except Exception as exc:
            with _google_lock:
                _google_run["error"] = str(exc)
            log.warning("wizard/google failed: %s", exc)
        finally:
            with _google_lock:
                _google_run["running"] = False

    threading.Thread(target=work, daemon=True, name="wizard-google").start()
    return {"started": True}


def console_urls() -> Dict[str, str]:
    """The exact pages for the clicks Google gives no API for."""
    project = db.get_sync_state(PROJECT_KEY) or ""
    suffix = f"?project={project}" if project else ""
    return {
        "overview": f"https://console.cloud.google.com/auth/overview{suffix}",
        "clients": f"https://console.cloud.google.com/auth/clients/create{suffix}",
        "audience": f"https://console.cloud.google.com/auth/audience{suffix}",
    }


def open_console() -> None:
    urls = console_urls()
    subprocess.run(["open", urls["clients"]], timeout=10)
    subprocess.run(["open", urls["audience"]], timeout=10)


# --------------------------------------------- the Downloads watcher
def ingest_client_secret() -> Optional[Dict[str, str]]:
    """Scan ~/Downloads for a fresh client_secret*.json; on sight, adopt the
    credentials, move the file out of Downloads (it is a secret), and hand
    back what changed. Called from every status poll while the Google step
    is open — the polling *is* the watcher."""
    candidates = sorted(DOWNLOADS.glob("client_secret*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        blob = data.get("installed") or data.get("web") or {}
        client_id, client_secret = blob.get("client_id"), blob.get("client_secret")
        if not client_id or not client_secret:
            continue
        write_env({"GOOGLE_CLIENT_ID": client_id,
                   "GOOGLE_CLIENT_SECRET": client_secret})
        kept = config_mod.ENV_FILE.parent / path.name
        try:
            shutil.move(str(path), kept)   # a secret does not live in Downloads
        except OSError:
            pass
        log.info("wizard: adopted OAuth client %s…", client_id[:20])
        return {"client_id": client_id}
    return None


def save_key(key: str) -> Dict[str, Any]:
    """Adopt the Anthropic key — verified by doing, not by prefix."""
    key = key.strip()
    if not re.match(r"^sk-ant-[A-Za-z0-9_\-]{20,}$", key):
        return {"ok": False, "reason": "that doesn't look like an Anthropic key (sk-ant-…)"}
    import httpx

    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5", "max_tokens": 1,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=20)
    except httpx.HTTPError as exc:
        return {"ok": False, "reason": f"couldn't reach Anthropic: {exc}"}
    if response.status_code in (200, 201):
        write_env({"ANTHROPIC_API_KEY": key})
        return {"ok": True}
    if response.status_code == 401:
        return {"ok": False, "reason": "Anthropic rejected that key"}
    # 4xx like overloaded/model quirks still prove the key authenticates.
    if response.status_code != 401 and response.status_code < 500:
        write_env({"ANTHROPIC_API_KEY": key})
        return {"ok": True}
    return {"ok": False, "reason": f"Anthropic answered {response.status_code} — try again"}


def open_fda_settings() -> None:
    subprocess.run(
        ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"],
        timeout=10)
