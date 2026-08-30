"""Google OAuth token lifecycle (§3, §12) — no live network.

Every httpx.post call is faked. The interesting behavior lives entirely in
token storage and refresh timing, none of which needs a real Google endpoint.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lifeline import db
from lifeline.config import Config, set_config
from lifeline.ingestion import google_auth


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def google_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFELINE_OFFLINE", "1")
    set_config(
        Config(
            db_path=tmp_path / "auth_test.db",
            offline_extraction=True,
            google_client_id="test-client-id",
            google_client_secret="test-client-secret",
        )
    )
    db.reset_connection()
    db.get_connection()
    yield
    db.reset_connection()


def fake_post(responses):
    """Returns a function that pops the next canned response, recording calls."""
    calls = []

    def _post(url, data=None, timeout=None):
        calls.append((url, data))
        return responses.pop(0)

    _post.calls = calls
    return _post


# --------------------------------------------------------- authorization
def test_authorization_url_requires_configuration(monkeypatch):
    set_config(Config(db_path=Config().db_path, offline_extraction=True))
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth.authorization_url()


def test_authorization_url_requests_offline_access_and_consent():
    url = google_auth.authorization_url()
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "client_id=test-client-id" in url


# -------------------------------------------------------------- exchange
def test_exchange_code_stores_both_tokens(monkeypatch):
    poster = fake_post(
        [FakeResponse(200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600, "scope": "a b"})]
    )
    monkeypatch.setattr(google_auth.httpx, "post", poster)

    google_auth.exchange_code("auth-code-123")

    record = db.get_oauth_token("google")
    assert record["access_token"] == "AT1"
    assert record["refresh_token"] == "RT1"
    assert record["scopes"] == ["a", "b"]
    assert poster.calls[0][1]["grant_type"] == "authorization_code"
    assert poster.calls[0][1]["code"] == "auth-code-123"


def test_exchange_code_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(google_auth.httpx, "post", fake_post([FakeResponse(400, text="invalid_grant")]))
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth.exchange_code("bad-code")
    assert db.get_oauth_token("google") is None


# --------------------------------------------------------------- refresh
def test_refresh_response_without_new_refresh_token_keeps_the_old_one(monkeypatch):
    """Google omits refresh_token on refresh responses — _store must not
    clobber the one already on file."""
    db.save_oauth_token("google", "stale-AT", "RT-original", "2020-01-01T00:00:00+00:00", [])
    monkeypatch.setattr(
        google_auth.httpx, "post", fake_post([FakeResponse(200, {"access_token": "AT-new", "expires_in": 3600})])
    )

    new_token = google_auth.refresh("RT-original")

    assert new_token == "AT-new"
    record = db.get_oauth_token("google")
    assert record["access_token"] == "AT-new"
    assert record["refresh_token"] == "RT-original"


def test_refresh_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(google_auth.httpx, "post", fake_post([FakeResponse(401, text="invalid_grant")]))
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth.refresh("some-refresh-token")


# ---------------------------------------------------------- access_token
def test_access_token_raises_when_never_connected():
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth.access_token()


def test_access_token_returns_cached_value_without_refreshing(monkeypatch):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    db.save_oauth_token("google", "still-good-AT", "RT", future, [])

    def explode(*a, **k):
        raise AssertionError("must not call the network when the cached token is still valid")

    monkeypatch.setattr(google_auth.httpx, "post", explode)

    assert google_auth.access_token() == "still-good-AT"


def test_access_token_refreshes_when_near_expiry(monkeypatch):
    soon = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(timespec="seconds")
    db.save_oauth_token("google", "about-to-expire-AT", "RT-1", soon, [])
    poster = fake_post([FakeResponse(200, {"access_token": "AT-refreshed", "expires_in": 3600})])
    monkeypatch.setattr(google_auth.httpx, "post", poster)

    token = google_auth.access_token()

    assert token == "AT-refreshed"
    assert len(poster.calls) == 1


def test_access_token_refreshes_when_already_expired(monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    db.save_oauth_token("google", "expired-AT", "RT-1", past, [])
    monkeypatch.setattr(
        google_auth.httpx, "post", fake_post([FakeResponse(200, {"access_token": "AT-fresh", "expires_in": 3600})])
    )
    assert google_auth.access_token() == "AT-fresh"


def test_access_token_raises_when_expired_with_no_refresh_token():
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    db.save_oauth_token("google", "expired-AT", None, past, [])
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth.access_token()


def test_access_token_tolerates_a_malformed_expiry(monkeypatch):
    """A corrupt stored expiry must fall through to a refresh, not crash."""
    db.save_oauth_token("google", "AT", "RT", "not-a-real-timestamp", [])
    monkeypatch.setattr(
        google_auth.httpx, "post", fake_post([FakeResponse(200, {"access_token": "AT-recovered", "expires_in": 3600})])
    )
    assert google_auth.access_token() == "AT-recovered"


# -------------------------------------------------------------- connected
def test_is_connected_false_with_no_record():
    assert google_auth.is_connected() is False


def test_is_connected_true_with_only_a_refresh_token():
    db.save_oauth_token("google", None, "RT-only", None, [])
    assert google_auth.is_connected() is True


def test_is_connected_true_with_only_an_access_token():
    db.save_oauth_token("google", "AT-only", None, None, [])
    assert google_auth.is_connected() is True


# ------------------------------------------------------------- authed_client
def test_authed_client_carries_a_bearer_header(monkeypatch):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    db.save_oauth_token("google", "bearer-value", "RT", future, [])
    with google_auth.authed_client() as client:
        assert client.headers["authorization"] == "Bearer bearer-value"


# --------------------------------------------------------------- disconnect
def test_disconnect_clears_stored_tokens_and_calls_revoke(monkeypatch):
    db.save_oauth_token("google", "AT", "RT", None, ["scope"])
    poster = fake_post([FakeResponse(200, {})])
    monkeypatch.setattr(google_auth.httpx, "post", poster)

    google_auth.disconnect()

    assert google_auth.is_connected() is False
    assert poster.calls[0][0] == "https://oauth2.googleapis.com/revoke"


def test_disconnect_tolerates_a_network_failure(monkeypatch):
    """§12 — purging local state must succeed even if Google is unreachable."""
    db.save_oauth_token("google", "AT", "RT", None, [])

    def explode(*a, **k):
        raise __import__("httpx").ConnectError("no network")

    monkeypatch.setattr(google_auth.httpx, "post", explode)

    google_auth.disconnect()   # must not raise
    assert google_auth.is_connected() is False


def test_disconnect_with_nothing_stored_is_a_safe_noop(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not call revoke when there is no token to revoke")

    monkeypatch.setattr(google_auth.httpx, "post", explode)
    google_auth.disconnect()
    assert google_auth.is_connected() is False
