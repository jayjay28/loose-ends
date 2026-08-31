"""Notification ingestion (§v3) — the apps we will never integrate.

These build a real `usernoted` database with real binary plists in the `data`
column, because those two things are the only part that is genuinely Apple's.
The store's schema is undocumented and allowed to drift, so the tests that
matter most here are the ones about *surviving* a shape we didn't expect.
"""
from __future__ import annotations

import plistlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lifeline import db
from lifeline.ingestion import notifications

from tests.conftest import make_person


def apple_time(when: datetime) -> float:
    return (when - notifications.APPLE_EPOCH).total_seconds()


def blob(title="", subtitle="", body="", nest=True) -> bytes:
    """A record payload shaped like the real thing: the words a couple of
    dicts down, under Apple's short keys."""
    words = {}
    if title:
        words["titl"] = title
    if subtitle:
        words["subt"] = subtitle
    if body:
        words["body"] = body
    payload = {"req": words, "app": "x"} if nest else words
    return plistlib.dumps(payload, fmt=plistlib.FMT_BINARY)


@pytest.fixture()
def store(tmp_path):
    """The store as macOS shapes it: an app table and a record table."""
    path = tmp_path / "db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR);
        CREATE TABLE record (
            rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB,
            request_date REAL, request_last_date REAL, delivered_date REAL,
            presented INTEGER, style INTEGER);
    """)
    conn.commit()
    conn.close()
    return path


def add(path: Path, bundle: str, data: bytes, when: datetime | None = None,
        rec_id: int | None = None) -> None:
    when = when or datetime.now(timezone.utc)
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT app_id FROM app WHERE identifier = ?", (bundle,)).fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO app (identifier) VALUES (?)", (bundle,))
        app_id = cur.lastrowid
    else:
        app_id = row[0]
    conn.execute(
        "INSERT INTO record (rec_id, app_id, data, delivered_date) VALUES (?,?,?,?)",
        (rec_id, app_id, data, apple_time(when)))
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ parsing
def test_the_words_come_out_of_the_binary_plist():
    parsed = notifications.parse_record(
        blob(title="Dev Shah", subtitle="#engineering", body="can you review the PR?"))
    assert parsed == {"title": "Dev Shah", "subtitle": "#engineering",
                      "body": "can you review the PR?"}


def test_the_words_are_found_however_deeply_they_are_nested():
    """The nesting around these keys has moved between macOS releases, so the
    reader searches for the key rather than walking a fixed path."""
    deep = plistlib.dumps({"a": {"b": [{"c": {"titl": "Mom", "body": "call me"}}]}},
                          fmt=plistlib.FMT_BINARY)
    parsed = notifications.parse_record(deep)
    assert parsed["title"] == "Mom" and parsed["body"] == "call me"


def test_a_payload_that_is_not_a_plist_is_empty_not_an_exception():
    assert notifications.parse_record(b"\x00 not a plist") == {
        "title": "", "subtitle": "", "body": ""}
    assert notifications.parse_record(None)["body"] == ""


def test_apple_epoch_dates_become_real_timestamps():
    when = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    assert notifications._when(apple_time(when)).startswith("2026-08-31T12:00:00")
    assert notifications._when(None) is None
    assert notifications._when("not a number") is None


# ------------------------------------------------------------- the schema
def test_an_unexpected_schema_is_logged_and_survived(tmp_path, caplog):
    """Apple's store, Apple's rules: a shape we don't recognise must produce
    nothing and a log line, never a stack trace."""
    path = tmp_path / "db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    assert notifications.read_store(path) == []
    assert notifications.poll(path=path) == 0


def test_a_store_without_an_app_table_still_reads(store):
    """The join is optional — losing app names must not lose the words."""
    add(store, "com.tinyspeck.slackmacgap", blob(title="Dev", body="ping"))
    conn = sqlite3.connect(store)
    conn.execute("DROP TABLE app")
    conn.commit()
    conn.close()

    found = notifications.read_store(store)
    assert len(found) == 1
    assert found[0]["body"] == "ping"
    assert found[0]["bundle"] == "unknown"


def test_an_absent_store_is_a_zero(tmp_path):
    assert notifications.poll(path=tmp_path / "nope") == 0


# ------------------------------------------------------------- what we keep
def test_a_person_saying_something_is_kept():
    assert notifications.is_worth_keeping(
        {"bundle": "com.tinyspeck.slackmacgap", "title": "Dev Shah",
         "body": "can you review the PR?"}) is True


def test_the_os_talking_to_itself_is_not():
    for bundle in ("com.apple.SoftwareUpdate", "com.apple.Music",
                   "com.spotify.client", "com.some.game"):
        assert notifications.is_worth_keeping(
            {"bundle": bundle, "title": "x", "body": "y"}) is False, bundle


def test_our_own_notifications_do_not_come_back_to_us():
    assert notifications.is_worth_keeping(
        {"bundle": "dev.clyon.looseends", "title": "Loose Ends",
         "body": "something moved"}) is False


def test_a_badge_in_words_is_not_a_loose_end():
    """"3 new items" with no body is a counter, not somebody speaking."""
    assert notifications.is_worth_keeping(
        {"bundle": "com.acme.app", "title": "3 new items", "body": ""}) is False


# ----------------------------------------------------------------- polling
def test_a_slack_message_becomes_a_stored_message(store):
    add(store, "com.tinyspeck.slackmacgap",
        blob(title="Dev Shah", subtitle="#eng", body="can you review the PR?"))

    assert notifications.poll(path=store) == 1
    stored = db.messages_since("2000-01-01", source="notification")
    assert len(stored) == 1
    assert "review the PR" in stored[0].text
    assert stored[0].metadata["app"] == "com.tinyspeck.slackmacgap"
    assert stored[0].metadata["glimpse"] is True, "downstream must know the ceiling"
    assert stored[0].is_from_user is False


def test_a_known_person_is_recognised_from_the_title(store):
    """A Slack ping should join that person's mail and messages, not start a
    stranger — the title is usually their name."""
    make_person("dev", "Dev Shah", handles=["dev@example.com"])
    add(store, "com.tinyspeck.slackmacgap", blob(title="Dev Shah", body="ping"))

    notifications.poll(path=store)
    assert db.messages_since("2000-01-01", source="notification")[0].person_id == "dev"


def test_each_app_is_its_own_conversation(store):
    add(store, "com.tinyspeck.slackmacgap", blob(title="Dev", body="ping"))
    add(store, "net.whatsapp.WhatsApp", blob(title="Tess", body="don't forget the thing"))

    assert notifications.poll(path=store) == 2
    conversations = {m.conversation_id for m
                     in db.messages_since("2000-01-01", source="notification")}
    assert conversations == {"notification:com.tinyspeck.slackmacgap",
                             "notification:net.whatsapp.WhatsApp"}


def test_the_same_notification_seen_twice_is_stored_once(store):
    """The buffer is sampled, not drained: a notification the user hasn't
    cleared is still there on the next poll, and must not double."""
    add(store, "com.tinyspeck.slackmacgap", blob(title="Dev", body="ping"))

    assert notifications.poll(path=store) == 1
    db.set_sync_state(notifications.CURSOR_KEY, "")   # force a full re-read
    assert notifications.poll(path=store) == 0
    assert len(db.messages_since("2000-01-01", source="notification")) == 1


def test_the_second_poll_only_reads_what_arrived_since(store):
    now = datetime.now(timezone.utc)
    add(store, "com.tinyspeck.slackmacgap", blob(title="Dev", body="first"),
        when=now - timedelta(hours=1))

    assert notifications.poll(path=store) == 1
    assert db.get_sync_state(notifications.CURSOR_KEY), "a high-water mark was kept"

    add(store, "com.tinyspeck.slackmacgap", blob(title="Dev", body="second"), when=now)
    assert notifications.poll(path=store) == 1
    assert len(db.messages_since("2000-01-01", source="notification")) == 2


def test_a_row_id_reused_by_the_store_does_not_collide(store):
    """`rec_id` is recycled as records are pruned and added, so identity has
    to be the words and the moment instead."""
    now = datetime.now(timezone.utc)
    add(store, "com.acme.app", blob(title="Acme", body="first thing"),
        when=now - timedelta(hours=2), rec_id=7)
    assert notifications.poll(path=store) == 1

    conn = sqlite3.connect(store)
    conn.execute("DELETE FROM record WHERE rec_id = 7")
    conn.commit()
    conn.close()
    add(store, "com.acme.app", blob(title="Acme", body="a different thing"),
        when=now, rec_id=7)

    assert notifications.poll(path=store) == 1, "the same row id, a new notification"
    assert len(db.messages_since("2000-01-01", source="notification")) == 2


def test_noise_never_reaches_the_store(store):
    add(store, "com.apple.Music", blob(title="Now Playing", body="a song"))
    add(store, "com.tinyspeck.slackmacgap", blob(title="Dev", body="ping"))

    assert notifications.poll(path=store) == 1
    apps = {m.metadata["app"] for m in db.messages_since("2000-01-01", source="notification")}
    assert apps == {"com.tinyspeck.slackmacgap"}


def test_seen_apps_can_answer_what_is_it_reading(store):
    """This source has to be able to show its work on demand — it is the
    creepiest-feeling thing the engine does."""
    add(store, "com.tinyspeck.slackmacgap", blob(title="Dev", body="one"))
    add(store, "com.tinyspeck.slackmacgap", blob(title="Dev", body="two"),
        when=datetime.now(timezone.utc) + timedelta(seconds=5))
    add(store, "net.whatsapp.WhatsApp", blob(title="Tess", body="three"))
    notifications.poll(path=store)

    seen = {row["app"]: row["n"] for row in notifications.seen_apps()}
    assert seen == {"com.tinyspeck.slackmacgap": 2, "net.whatsapp.WhatsApp": 1}


def test_a_locked_store_never_raises_out_of_read_store(tmp_path, monkeypatch):
    """Without Full Disk Access macOS refuses at *open* time with
    `DatabaseError: authorization denied` — not PermissionError, and not at
    query time. `read_store` promises a list to every caller regardless."""
    import sqlite3 as sq

    blocked = tmp_path / "db"
    blocked.write_bytes(b"")

    def denied(*_a, **_kw):
        raise sq.DatabaseError("authorization denied")

    monkeypatch.setattr(notifications.sqlite3, "connect", denied)
    assert notifications.read_store(blocked) == []
    assert notifications.readable() is False
    assert notifications.poll(path=blocked) == 0


def test_the_reader_can_be_switched_off(store, monkeypatch):
    """The creepiest source in the engine needs an off switch that works
    without uninstalling anything."""
    add(store, "com.tinyspeck.slackmacgap", blob(title="Dev", body="ping"))
    monkeypatch.setenv("LIFELINE_NO_NOTIFICATIONS", "1")
    assert notifications.poll(path=store) == 0
    assert db.messages_since("2000-01-01", source="notification") == []
