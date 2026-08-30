"""Google OAuth 2.0, read-only (§3, §12).

Only tokens are stored — never credentials. Access tokens are refreshed
lazily and the refresh token is the only long-lived secret at rest.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import httpx

from .. import db
from ..config import GOOGLE_SCOPES, get_config

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
PROVIDER = "google"


class GoogleAuthError(RuntimeError):
    pass


def authorization_url(state: str = "lifeline") -> str:
    cfg = get_config()
    if not cfg.has_google:
        raise GoogleAuthError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not configured")
    params = {
        "client_id": cfg.google_client_id,
        "redirect_uri": cfg.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",       # we need a refresh token for polling
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> Dict[str, str]:
    cfg = get_config()
    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "code": code,
            "client_id": cfg.google_client_id,
            "client_secret": cfg.google_client_secret,
            "redirect_uri": cfg.google_redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise GoogleAuthError(f"token exchange failed: {response.status_code} {response.text}")
    payload = response.json()
    _store(payload)
    return payload


def _store(payload: Dict[str, object], keep_refresh: Optional[str] = None) -> None:
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in", 3600)))
    scopes = str(payload.get("scope", " ".join(GOOGLE_SCOPES))).split()
    db.save_oauth_token(
        provider=PROVIDER,
        access_token=str(payload.get("access_token")),
        # Google omits refresh_token on refresh responses; never clobber it.
        refresh_token=str(payload.get("refresh_token") or keep_refresh or "") or None,
        token_expiry=expiry.isoformat(timespec="seconds"),
        scopes=scopes,
    )


def refresh(refresh_token: str) -> str:
    cfg = get_config()
    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "refresh_token": refresh_token,
            "client_id": cfg.google_client_id,
            "client_secret": cfg.google_client_secret,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise GoogleAuthError(f"token refresh failed: {response.status_code} {response.text}")
    payload = response.json()
    _store(payload, keep_refresh=refresh_token)
    return str(payload["access_token"])


def access_token() -> str:
    """Current access token, refreshed if it is within 60s of expiry."""
    record = db.get_oauth_token(PROVIDER)
    if not record:
        raise GoogleAuthError("Google account not connected — run the OAuth flow first")
    expiry = record.get("token_expiry")
    if record.get("access_token") and expiry:
        try:
            deadline = datetime.fromisoformat(expiry)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline - datetime.now(timezone.utc) > timedelta(seconds=60):
                return str(record["access_token"])
        except ValueError:
            pass
    if not record.get("refresh_token"):
        raise GoogleAuthError("access token expired and no refresh token is stored — re-authorize")
    return refresh(str(record["refresh_token"]))


def is_connected() -> bool:
    record = db.get_oauth_token(PROVIDER)
    return bool(record and (record.get("access_token") or record.get("refresh_token")))


def has_scope(keyword: str) -> bool:
    """Whether the granted token actually carries a scope (e.g. 'calendar').
    Requesting a scope isn't the same as being granted it at the consent screen."""
    record = db.get_oauth_token(PROVIDER)
    scopes = record.get("scopes", []) if record else []
    return any(keyword in s for s in scopes)


def authed_client() -> httpx.Client:
    return httpx.Client(
        headers={"Authorization": f"Bearer {access_token()}"},
        timeout=30,
    )


def disconnect() -> None:
    """§12 — revoke and forget."""
    record = db.get_oauth_token(PROVIDER)
    token = (record or {}).get("refresh_token") or (record or {}).get("access_token")
    if token:
        try:
            httpx.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=15)
        except httpx.HTTPError:
            pass
    db.save_oauth_token(PROVIDER, None, None, None, [])
