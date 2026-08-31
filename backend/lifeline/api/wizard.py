"""§v3 workstream 2 — the setup wizard's engine room.

The wizard lives by three rules (the signed wireframes state them):

  * **It advances itself.** Every wait names what it's watching — a Settings
    grant, a key, a phone — and the status poll flips forward the moment the
    thing happens. No "Next" button that might lie.
  * **Say who does each minute.** Steps are marked ours or the user's.
  * **Verify by doing.** "Messages readable" means chat.db actually opened.
    Every ✓ is a receipt.

This module is the doing: live checks, and the one place credentials get
written — `~/.lifeline/env`, never a shell rc file. The routes in `app.py`
are thin wrappers; the page is `setup_page.html`.

**§v3 deleted the Google half of this file.** It automated everything Google
allowed (sign-in, project creation, API enables, deep links, a Downloads
watcher for `client_secret_*.json`) and still could not automate the middle,
because there is no public API for creating an OAuth client. Two real
walkthroughs ended there. Mail now comes from the local Mail store behind the
Full Disk Access grant Messages needs anyway, so the whole detour — and the
consent screen, the unverified-app warning, and the per-user cloud project
behind it — is gone rather than merely reordered.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict

from .. import db
from .. import config as config_mod
from ..config import get_config
from ..ingestion import applemail

log = logging.getLogger(__name__)


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


_restart_scheduled = False


def _fda_probe() -> Dict[str, bool]:
    """The check, twice: in this process, then from a fresh child. macOS
    caches a TCC denial against the running process, so the grant the user
    just flipped can be invisible to the engine until it relaunches — field
    report #1: "I added zsh to FDA but it didn't update." The child sees the
    new truth; readable=False with granted=True means: restart me."""
    if _fda_readable():
        return {"readable": True, "granted_pending_restart": False}
    chat = Path.home() / "Library" / "Messages" / "chat.db"
    try:
        probe = subprocess.run(
            ["/bin/zsh", "-c", f"head -c 16 '{chat}' >/dev/null"],
            capture_output=True, timeout=10)
        granted = probe.returncode == 0
    except (subprocess.SubprocessError, OSError):
        granted = False
    return {"readable": False, "granted_pending_restart": granted}


def _fda_check_and_maybe_restart() -> Dict[str, bool]:
    """What `status()` reports for the FDA step — and the self-advance: when
    the grant is in but this process can't see it, a managed engine restarts
    itself rather than asking the user to notice."""
    fda = _fda_probe()
    if fda["granted_pending_restart"]:
        _restart_to_adopt_grant()
    return fda


def _restart_to_adopt_grant() -> None:
    """Exit so launchd brings the engine back with fresh TCC eyes. Only when
    the installer's job manages us (LIFELINE_MANAGED=1) — exiting a dev
    `uvicorn` would just be dying. The wizard page polls through the gap;
    the step state machine was built resumable for exactly this shape."""
    global _restart_scheduled
    if _restart_scheduled or os.environ.get("LIFELINE_MANAGED") != "1":
        return
    _restart_scheduled = True
    log.info("wizard: FDA granted but invisible to this process — restarting")
    threading.Timer(1.5, os._exit, args=(0,)).start()


def _reachable_url() -> str:
    """Where the phone should point. §v3 ws4 moved the knowledge of the
    engine's doors into `transport`; this stays as the wizard's one call."""
    from . import transport

    return transport.reachable_url()


def status() -> Dict[str, Any]:
    """The whole board, computed live. Every ✓ here is a receipt.

    §v3 dropped the Google step entirely: mail now comes from the local Mail
    store behind the same Full Disk Access grant Messages needs, so setup is
    three local things — a permission, a key, a phone — and nothing that
    involves a browser tab, a cloud console, or an account with anyone.
    """
    cfg = get_config()
    return {
        "fda": _fda_check_and_maybe_restart(),
        "mail": {
            "readable": applemail.available(),
            # "no mail found" has two very different fixes; say which.
            "needs_permission": applemail.permission_denied(),
        },
        "key": {"present": bool(cfg.anthropic_api_key)},
        "pairing": {
            "devices": len(db.list_devices()),
            "reach_url": _reachable_url(),
        },
    }


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
