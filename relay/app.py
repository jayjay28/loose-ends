"""The push relay — the one server Loose Ends ever runs (§v3 workstream 5).

Self-hosted engines can't sign APNs pushes: that takes the app's private
key, and shipping the key to every stranger's Mac would let anyone push to
anyone. So this exists — a stateless forwarder that signs with the key and
knows as little as a forwarder can.

**Content-free by construction.** The push API has no title field and no
body field; there is nothing an engine could send that would put a user's
words through this server. Every push it signs says the same fixed sentence,
marked `mutable-content` so the phone's notification extension replaces it
with words fetched from the user's own engine — the relay carries the knock,
never the message. Opaque ids ride along so the phone knows which door.

**Stateless on purpose.** An install's secret is an HMAC of its id under the
relay's key, so registration writes nothing and verification recomputes —
the relay can scale to zero, restart, or be rebuilt with no database to
lose. Revocation is a denylist in the environment; per-install rate limits
live in memory and reset on restart, which bounds abuse without inventing
state to manage.

Run: uvicorn app:app · Env: RELAY_SIGNING_KEY, APNS_KEY_PATH, APNS_KEY_ID,
APNS_TEAM_ID, APNS_TOPIC (default com.lifelinecly.app), APNS_SANDBOX=0|1.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
import uuid
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("relay")

app = FastAPI(title="Loose Ends push relay", version="1.0.0")

PRODUCTION_HOST = "https://api.push.apple.com"
SANDBOX_HOST = "https://api.sandbox.push.apple.com"
_TOKEN_TTL = 45 * 60

# What every push through here says. Fixed — not configurable, not a field.
PLACEHOLDER_TITLE = "Loose Ends"
PLACEHOLDER_BODY = "Something moved on an end you're carrying."

# Hex, sanity-bounded. Apple says not to assume token length — 64 hex is
# today's phones, and the simulator already proves longer ones exist.
_DEVICE_TOKEN = re.compile(r"^[0-9a-f]{16,200}$", re.IGNORECASE)
_LEVELS = {"passive", "active", "time-sensitive"}

# Rate limits: enough for a chatty engine, hostile to a spammer. In-memory —
# a restart forgives, which is the right failure mode for a limiter.
PER_INSTALL_PER_HOUR = 60
PER_DEVICE_PER_HOUR = 120
_sends: Dict[str, Deque[float]] = defaultdict(deque)


def _signing_key() -> str:
    key = os.environ.get("RELAY_SIGNING_KEY", "")
    if not key:
        raise RuntimeError("RELAY_SIGNING_KEY is not set")
    return key


def _secret_for(install_id: str) -> str:
    return hmac.new(_signing_key().encode(), install_id.encode(),
                    hashlib.sha256).hexdigest()


def _denied(install_id: str) -> bool:
    return install_id in os.environ.get("RELAY_DENYLIST", "").split(",")


def _over_limit(key: str, cap: int) -> bool:
    now = time.time()
    window = _sends[key]
    while window and now - window[0] > 3600:
        window.popleft()
    if len(window) >= cap:
        return True
    window.append(now)
    return False


# -------------------------------------------------------------------- APNs
_cached_jwt: Optional[tuple] = None


def _provider_token() -> str:
    global _cached_jwt
    if _cached_jwt and time.time() - _cached_jwt[1] < _TOKEN_TTL:
        return _cached_jwt[0]
    import jwt

    with open(os.environ["APNS_KEY_PATH"]) as handle:
        signing_key = handle.read()
    issued = time.time()
    token = jwt.encode(
        {"iss": os.environ["APNS_TEAM_ID"], "iat": int(issued)},
        signing_key, algorithm="ES256",
        headers={"kid": os.environ["APNS_KEY_ID"]},
    )
    _cached_jwt = (token, issued)
    return token


def _apns_send(tokens: List[str], payload: dict, priority: str,
               collapse_id: Optional[str]) -> Dict[str, str]:
    host = SANDBOX_HOST if os.environ.get("APNS_SANDBOX", "0") == "1" else PRODUCTION_HOST
    headers = {
        "authorization": f"bearer {_provider_token()}",
        "apns-topic": os.environ.get("APNS_TOPIC", "com.lifelinecly.app"),
        "apns-push-type": "alert",
        "apns-priority": priority,
    }
    if collapse_id:
        headers["apns-collapse-id"] = collapse_id[:64]
    results: Dict[str, str] = {}
    with httpx.Client(http2=True, timeout=20) as client:
        for token in tokens:
            try:
                r = client.post(f"{host}/3/device/{token}", json=payload, headers=headers)
                results[token] = "ok" if r.status_code == 200 else f"{r.status_code}: {r.text}"
            except httpx.HTTPError as exc:
                results[token] = f"error: {exc}"
    for token, status in results.items():
        if status != "ok":
            log.warning("apns delivery failed (%s…): %s", token[:8], status)
    return results


# --------------------------------------------------------------- the routes
class PushIn(BaseModel):
    device_tokens: List[str] = Field(min_length=1, max_length=10)
    level: str = "active"                      # passive | active | time-sensitive
    collapse_id: Optional[str] = None
    thread_id: Optional[str] = None            # opaque ids for routing — not words
    finding_id: Optional[str] = None


def _install_from(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "register, then send Bearer <install_id>.<secret>")
    try:
        install_id, secret = authorization[7:].split(".", 1)
    except ValueError:
        raise HTTPException(401, "malformed credentials")
    if _denied(install_id) or not hmac.compare_digest(secret, _secret_for(install_id)):
        raise HTTPException(401, "unknown install")
    return install_id


@app.post("/v1/register")
def register() -> dict:
    """Mint install credentials. Stateless: the secret is provably ours."""
    install_id = uuid.uuid4().hex
    return {"install_id": install_id, "install_secret": _secret_for(install_id)}


@app.post("/v1/push")
def push(body: PushIn, authorization: Optional[str] = Header(None)) -> dict:
    install_id = _install_from(authorization)

    if body.level not in _LEVELS:
        raise HTTPException(400, f"level must be one of {sorted(_LEVELS)}")
    bad = [t for t in body.device_tokens if not _DEVICE_TOKEN.match(t)]
    if bad:
        raise HTTPException(400, "device tokens are 64 hex characters")
    if _over_limit(f"i:{install_id}", PER_INSTALL_PER_HOUR):
        raise HTTPException(429, "install rate limit reached — the briefing lane exists for a reason")
    for token in body.device_tokens:
        if _over_limit(f"d:{token.lower()}", PER_DEVICE_PER_HOUR):
            raise HTTPException(429, "device rate limit reached")

    payload: dict = {
        "aps": {
            "alert": {"title": PLACEHOLDER_TITLE, "body": PLACEHOLDER_BODY},
            "mutable-content": 1,
            "interruption-level": body.level,
        },
        "relay": 1,
    }
    if body.level == "time-sensitive":
        payload["aps"]["sound"] = "default"
    if body.thread_id:
        payload["thread_id"] = body.thread_id
    if body.finding_id:
        payload["finding_id"] = body.finding_id

    priority = "5" if body.level == "passive" else "10"
    results = _apns_send(body.device_tokens, payload, priority, body.collapse_id)
    return {"results": results}


# /v1, not /healthz: Google's frontend reserves /healthz on run.app domains
# and answers it with its own 404 before the container ever sees it.
@app.get("/v1/health")
def health() -> dict:
    return {"ok": True}
