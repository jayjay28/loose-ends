"""APNs delivery (§8.4).

Interruption levels map straight onto Apple's own: Time-Sensitive can break
through Focus, Active is the default, and Passive never pushes at all — it is
only ever delivered in-app or inside the batched digest.

Token-based auth (p8 key, ES256 JWT). Without credentials the sender runs in
dry-run mode and just logs, so the queueing logic stays testable.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx

from ..config import get_config
from ..models import InterruptionLevel

log = logging.getLogger(__name__)

PRODUCTION_HOST = "https://api.push.apple.com"
SANDBOX_HOST = "https://api.sandbox.push.apple.com"
_TOKEN_TTL = 45 * 60      # Apple rejects provider tokens older than an hour.

# Lifeline's levels -> APNs interruption-level strings.
APNS_LEVEL = {
    InterruptionLevel.TIME_SENSITIVE: "time-sensitive",
    InterruptionLevel.ACTIVE: "active",
    InterruptionLevel.PASSIVE: "passive",
}

_cached_token: Optional[Tuple[str, float]] = None


class APNsError(RuntimeError):
    pass


def _provider_token() -> str:
    global _cached_token
    if _cached_token and time.time() - _cached_token[1] < _TOKEN_TTL:
        return _cached_token[0]

    import jwt

    cfg = get_config()
    with open(cfg.apns_key_path, "r") as handle:
        signing_key = handle.read()
    issued = time.time()
    token = jwt.encode(
        {"iss": cfg.apns_team_id, "iat": int(issued)},
        signing_key,
        algorithm="ES256",
        headers={"kid": cfg.apns_key_id},
    )
    _cached_token = (token, issued)
    return token


def build_payload(
    title: str,
    body: str,
    interruption: str,
    item_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    badge: Optional[int] = None,
    thread_id: Optional[str] = None,
    finding_id: Optional[str] = None,
) -> Dict[str, object]:
    alert: Dict[str, object] = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default" if interruption == InterruptionLevel.TIME_SENSITIVE else None,
            "interruption-level": APNS_LEVEL.get(interruption, "active"),
            # Grouping keeps a passive digest from stacking over a real alert.
            "thread-id": conversation_id or interruption,
            "relevance-score": {"time_sensitive": 1.0, "active": 0.6, "passive": 0.2}.get(interruption, 0.5),
        }
    }
    if badge is not None:
        alert["aps"]["badge"] = badge          # type: ignore[index]
    if alert["aps"]["sound"] is None:          # type: ignore[index]
        del alert["aps"]["sound"]              # type: ignore[index]
    if item_id:
        alert["item_id"] = item_id
    # Where a tap should land. Without these the notification is a dead
    # end: it tells the user something specific, then drops them wherever
    # the app happened to be.
    if thread_id:
        alert["thread_id"] = thread_id
    if finding_id:
        alert["finding_id"] = finding_id
    return alert


def send(device_tokens: List[str], payload: Dict[str, object], collapse_id: Optional[str] = None) -> Dict[str, str]:
    """Deliver to each device. Returns token -> status ("ok" or an error)."""
    cfg = get_config()
    results: Dict[str, str] = {}

    if not cfg.has_apns:
        log.info("APNs not configured — dry run: %s", payload)
        return {token: "dry-run" for token in device_tokens}
    if not device_tokens:
        return results

    host = SANDBOX_HOST if cfg.apns_sandbox else PRODUCTION_HOST
    headers = {
        "authorization": f"bearer {_provider_token()}",
        "apns-topic": cfg.apns_topic,
        "apns-push-type": "alert",
        "apns-priority": "10" if payload.get("aps", {}).get("interruption-level") != "passive" else "5",  # type: ignore[union-attr]
    }
    if collapse_id:
        headers["apns-collapse-id"] = collapse_id[:64]

    with httpx.Client(http2=True, timeout=20) as client:
        for token in device_tokens:
            try:
                response = client.post(f"{host}/3/device/{token}", json=payload, headers=headers)
                results[token] = "ok" if response.status_code == 200 else f"{response.status_code}: {response.text}"
            except httpx.HTTPError as exc:
                results[token] = f"error: {exc}"
    for token, status in results.items():
        # `flush` marks the row sent either way, so a failure that isn't
        # logged here never surfaces anywhere — a week of pushes went to
        # nobody before this line existed.
        if status != "ok":
            log.warning("apns delivery failed (%s…): %s", token[:8], status)
    return results
