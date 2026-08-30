"""The engine's side of the push relay (§v3 workstream 5).

An engine with its own APNs key — the developer's machine — pushes directly,
words and all, and never comes here. Every other engine has no key and no
way to mint one, so it knocks through the relay: register once (credentials
kept in sync_state like any other integration), then send *ids, never
words*. The relay's API has no field for content, which makes the privacy
promise structural rather than behavioral — this module couldn't leak a
notification body if it tried.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import httpx

from .. import db
from ..config import get_config

log = logging.getLogger(__name__)

INSTALL_ID_KEY = "relay:install_id"
INSTALL_SECRET_KEY = "relay:install_secret"


def configured() -> bool:
    return bool(get_config().push_relay_url)


def _base() -> str:
    return get_config().push_relay_url.rstrip("/")


def _credentials() -> Optional[str]:
    """This install's bearer, registering on first need."""
    install_id = db.get_sync_state(INSTALL_ID_KEY)
    secret = db.get_sync_state(INSTALL_SECRET_KEY)
    if install_id and secret:
        return f"{install_id}.{secret}"
    try:
        with httpx.Client(timeout=15) as client:
            body = client.post(f"{_base()}/v1/register").json()
    except httpx.HTTPError as exc:
        log.warning("relay registration failed: %s", exc)
        return None
    db.set_sync_state(INSTALL_ID_KEY, body["install_id"])
    db.set_sync_state(INSTALL_SECRET_KEY, body["install_secret"])
    log.info("registered with push relay as %s", body["install_id"][:8])
    return f"{body['install_id']}.{body['install_secret']}"


def send(device_tokens: List[str], *, level: str, collapse_id: Optional[str] = None,
         thread_id: Optional[str] = None, finding_id: Optional[str] = None) -> Dict[str, str]:
    """Ask the relay to knock. Ids only — the phone fetches the words."""
    bearer = _credentials()
    if bearer is None:
        return {t: "relay unreachable" for t in device_tokens}
    try:
        with httpx.Client(timeout=20) as client:
            response = client.post(
                f"{_base()}/v1/push",
                headers={"Authorization": f"Bearer {bearer}"},
                json={
                    "device_tokens": device_tokens,
                    "level": level,
                    "collapse_id": collapse_id,
                    "thread_id": thread_id,
                    "finding_id": finding_id,
                },
            )
    except httpx.HTTPError as exc:
        log.warning("relay push failed: %s", exc)
        return {t: f"error: {exc}" for t in device_tokens}
    if response.status_code != 200:
        log.warning("relay push refused: %s %s", response.status_code, response.text[:200])
        return {t: f"{response.status_code}" for t in device_tokens}
    return response.json().get("results", {})
