"""§v3 workstream 3 — the API stops being open.

For two years of prototypes the API trusted whoever could reach it, and
"whoever could reach it" was defined by the network: loopback only, with
Tailscale as the fence. Onboarding strangers ends that — the engine will sit
on home Wi-Fi, and a fence made of network shape doesn't survive other
people's networks.

The rule, in one breath: **a request is either from this Mac, or it carries a
token a pairing minted.**

*From this Mac* means genuinely local — the socket peer is loopback AND no
forwarding header rides along. Tailscale serve terminates on this machine and
proxies to loopback, but it stamps `X-Forwarded-For` on everything it relays,
so proxied phones don't inherit the Mac's trust. The CLI, the setup wizard,
and launchd health checks stay credential-free because they really are the
machine talking to itself.

Tokens are opaque bearers (`le_<id>_<secret>`); the database keeps only the
sha256 of the secret. Pairing is the only mint: a short-lived, single-use,
human-typeable code created from a trusted context, claimed once by a device,
spent forever.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import db
from ..models import now_iso, parse_iso

log = logging.getLogger(__name__)

PAIRING_TTL_MINUTES = 10
# Unambiguous alphabet — no 0/O, 1/I/L. Eight characters, typed by a person
# squinting at a screen across the room.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LENGTH = 8

# Paths a stranger may touch without a token. Each is here because a token is
# impossible at that moment, not merely inconvenient.
OPEN_PATHS = frozenset({
    "/pair/claim",             # the bootstrap — this is how a token is born
})

_FORWARDING_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded")


# ------------------------------------------------------------------- tokens
def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def mint_token(device_name: str = "") -> dict:
    """Create a bearer token. Returns the one time the secret is visible."""
    token_id = secrets.token_hex(4)
    secret = secrets.token_urlsafe(24)
    db.get_connection().execute(
        "INSERT INTO api_tokens (id, secret_hash, device_name, created_at) VALUES (?, ?, ?, ?)",
        (token_id, _hash(secret), device_name, now_iso()),
    )
    db.get_connection().commit()
    return {"token": f"le_{token_id}_{secret}", "token_id": token_id}


def verify_token(bearer: str) -> Optional[str]:
    """The token's id when the bearer is live, else None."""
    parts = bearer.split("_", 2)
    if len(parts) != 3 or parts[0] != "le":
        return None
    _, token_id, secret = parts
    row = db.get_connection().execute(
        "SELECT secret_hash, revoked_at FROM api_tokens WHERE id = ?", (token_id,)
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    if not secrets.compare_digest(row["secret_hash"], _hash(secret)):
        return None
    db.get_connection().execute(
        "UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now_iso(), token_id)
    )
    db.get_connection().commit()
    return token_id


def revoke_token(token_id: str) -> bool:
    cur = db.get_connection().execute(
        "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (now_iso(), token_id),
    )
    db.get_connection().commit()
    return cur.rowcount > 0


def list_tokens() -> list:
    rows = db.get_connection().execute(
        "SELECT id, device_name, created_at, last_used_at, revoked_at FROM api_tokens "
        "ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ pairing
def start_pairing() -> dict:
    """Mint a pairing code. Short-lived, single-use, shown on the Mac."""
    now = datetime.now(timezone.utc)
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
    db.get_connection().execute(
        "INSERT INTO pairing_codes (code, created_at, expires_at) VALUES (?, ?, ?)",
        (code, now.isoformat(timespec="seconds"),
         (now + timedelta(minutes=PAIRING_TTL_MINUTES)).isoformat(timespec="seconds")),
    )
    db.get_connection().commit()
    return {"code": code, "expires_in_minutes": PAIRING_TTL_MINUTES}


def claim_pairing(code: str, device_name: str = "") -> Optional[dict]:
    """Spend a pairing code for a token. One claim, ever, per code."""
    row = db.get_connection().execute(
        "SELECT code, expires_at, claimed_at FROM pairing_codes WHERE code = ?",
        (code.strip().upper(),),
    ).fetchone()
    if row is None or row["claimed_at"] is not None:
        return None
    expires = parse_iso(row["expires_at"])
    if expires is None or datetime.now(timezone.utc) > expires:
        return None
    minted = mint_token(device_name)
    db.get_connection().execute(
        "UPDATE pairing_codes SET claimed_at = ?, token_id = ? WHERE code = ?",
        (now_iso(), minted["token_id"], row["code"]),
    )
    db.get_connection().commit()
    log.info("pairing claimed by %r -> token %s", device_name or "unnamed device",
             minted["token_id"])
    return minted


def pairing_status(code: str) -> Optional[dict]:
    """For the wizard's screen: has this code been claimed yet?"""
    row = db.get_connection().execute(
        "SELECT claimed_at, expires_at FROM pairing_codes WHERE code = ?",
        (code.strip().upper(),),
    ).fetchone()
    if row is None:
        return None
    return {"claimed": row["claimed_at"] is not None, "expires_at": row["expires_at"]}


# ---------------------------------------------------------------- the gate
def is_trusted_local(request: Request) -> bool:
    """Genuinely this machine: loopback peer, nothing forwarded.

    Tailscale serve (and any reverse proxy) connects from loopback but stamps
    forwarding headers on what it relays — that is exactly the difference
    between the Mac talking to itself and the world arriving through a door.
    """
    host = request.client.host if request.client else ""
    # "testclient" is starlette's in-process TestClient peer. A socket can
    # never report it — uvicorn hands us real addresses — so trusting it
    # changes nothing in production and spares six hundred tests a handshake.
    if host not in ("127.0.0.1", "::1", "testclient"):
        return False
    return not any(h in request.headers for h in _FORWARDING_HEADERS)


async def gate(request: Request, call_next):
    """The middleware. Local, tokened, or 401 — nothing else."""
    if request.url.path in OPEN_PATHS or request.method == "OPTIONS":
        return await call_next(request)
    if is_trusted_local(request):
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and verify_token(header[7:]) is not None:
        return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"detail": "pair this device: get a code from the Mac and claim it"},
    )
