"""WhatsApp's live store (§3) — the desktop app's own database.

Built against the real Core Data shape (`ZWAMESSAGE`, `ZWACHATSESSION`,
Apple-epoch dates, JIDs), because that is the part that is genuinely
WhatsApp's and the part allowed to change under us. The tests that matter
most are the ones about surviving a schema we didn't expect.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lifeline import db
from lifeline.ingestion import whatsapp

from tests.conftest import make_person


def apple_time(when: datetime) -> float:
    return (when - whatsapp.APPLE_EPOCH).total_seconds()


@pytest.fixture()
def store(tmp_path) -> Path:
    """ChatStorage.sqlite as WhatsApp shapes it — the columns the reader
    actually asks for, under their real names."""
    path = tmp_path / "ChatStorage.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ZWACHATSESSION (
            Z_PK INTEGER PRIMARY KEY, ZCONTACTJID VARCHAR,
            ZPARTNERNAME VARCHAR, ZLASTMESSAGEDATE TIMESTAMP);
        CREATE TABLE ZWAMESSAGE (
            Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
            ZMESSAGEDATE TIMESTAMP, ZTEXT VARCHAR, ZFROMJID VARCHAR,
            ZTOJID VARCHAR, ZPUSHNAME VARCHAR);
    """)
    conn.commit()
    conn.close()
    return path


def chat(path: Path, pk: int, jid: str, partner: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME) "
                 "VALUES (?,?,?)", (pk, jid, partner))
    conn.commit()
    conn.close()


def message(path: Path, pk: int, session: int, text: str, *, from_me: bool = False,
            when: datetime | None = None, from_jid: str = "", push: str = "") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, "
        "ZTEXT, ZFROMJID, ZPUSHNAME) VALUES (?,?,?,?,?,?,?)",
        (pk, session, 1 if from_me else 0,
         apple_time(when or datetime.now(timezone.utc)), text, from_jid, push))
    conn.commit()
    conn.close()


# ------------------------------------------------------------------- JIDs
def test_a_jid_becomes_the_same_handle_every_other_source_uses():
    """This is the whole point of normalising: a WhatsApp thread and an
    iMessage thread have to land on one person, not two."""
    assert whatsapp._jid_handle("14155550142@s.whatsapp.net") == "4155550142"
    assert whatsapp._jid_handle("4155550142@s.whatsapp.net") == "4155550142"
    assert whatsapp._jid_handle("") == ""


def test_a_group_jid_is_recognised_and_stripped():
    assert whatsapp._is_group("1234567890-1600000000@g.us") is True
    assert whatsapp._is_group("14155550142@s.whatsapp.net") is False
    assert whatsapp._jid_handle("1234567890-1600000000@g.us") == "1234567890"


def test_apple_epoch_dates_become_real_timestamps():
    when = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    assert whatsapp._when(apple_time(when)).startswith("2026-08-31T12:00:00")
    assert whatsapp._when(None) is None
    assert whatsapp._when("not a number") is None


# ----------------------------------------------------------- the schema
def test_an_unexpected_schema_is_logged_and_survived(tmp_path):
    """WhatsApp's store, WhatsApp's rules: a shape we don't recognise gives
    nothing and a log line, never a stack trace."""
    path = tmp_path / "ChatStorage.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    assert whatsapp.read_store(path) == []
    assert whatsapp.poll(path=path) == 0


def test_a_store_without_the_session_table_still_reads(store):
    """Losing chat titles must not lose the messages."""
    chat(store, 1, "14155550142@s.whatsapp.net", "Dev Shah")
    message(store, 10, 1, "are we still on for thursday?",
            from_jid="14155550142@s.whatsapp.net")
    conn = sqlite3.connect(store)
    conn.execute("DROP TABLE ZWACHATSESSION")
    conn.commit()
    conn.close()

    rows = whatsapp.read_store(store)
    assert len(rows) == 1 and rows[0]["text"] == "are we still on for thursday?"


def test_an_absent_store_is_a_zero(tmp_path):
    assert whatsapp.poll(path=tmp_path / "nope.sqlite") == 0


def test_a_locked_store_never_raises(tmp_path, monkeypatch):
    blocked = tmp_path / "ChatStorage.sqlite"
    blocked.write_bytes(b"")

    def denied(*_a, **_kw):
        raise sqlite3.DatabaseError("authorization denied")

    monkeypatch.setattr(whatsapp.sqlite3, "connect", denied)
    assert whatsapp.read_store(blocked) == []


# ---------------------------------------------------------------- polling
def test_a_message_from_a_friend_is_stored(store):
    make_person("dev", "Dev Shah", handles=["4155550142"])
    chat(store, 1, "14155550142@s.whatsapp.net", "Dev Shah")
    message(store, 10, 1, "are we still on for thursday?",
            from_jid="14155550142@s.whatsapp.net")

    assert whatsapp.poll(path=store) == 1
    stored = db.messages_since("2000-01-01", source="whatsapp")
    assert len(stored) == 1
    assert stored[0].text == "are we still on for thursday?"
    assert stored[0].person_id == "dev", "the JID resolved to the person we know"
    assert stored[0].is_from_user is False


def test_my_own_replies_are_kept_as_evidence(store):
    """The completion engine closes loops on the strength of my replies."""
    chat(store, 1, "14155550142@s.whatsapp.net", "Dev Shah")
    message(store, 10, 1, "yes, booked it", from_me=True)

    assert whatsapp.poll(path=store) == 1
    assert db.messages_since("2000-01-01", source="whatsapp")[0].is_from_user is True


def test_the_chat_jid_names_the_thread_not_the_display_name(store):
    """A contact who renames themselves must not fork the conversation."""
    chat(store, 1, "14155550142@s.whatsapp.net", "Dev Shah")
    message(store, 10, 1, "first", from_jid="14155550142@s.whatsapp.net")
    assert whatsapp.poll(path=store) == 1

    conn = sqlite3.connect(store)
    conn.execute("UPDATE ZWACHATSESSION SET ZPARTNERNAME = 'Dev (work)' WHERE Z_PK = 1")
    conn.commit()
    conn.close()
    message(store, 11, 1, "second", from_jid="14155550142@s.whatsapp.net")
    whatsapp.poll(path=store)

    threads = {m.conversation_id for m in db.messages_since("2000-01-01", source="whatsapp")}
    assert threads == {"whatsapp:14155550142@s.whatsapp.net"}, "one thread, renamed"


def test_a_group_chat_is_marked_as_one(store):
    chat(store, 1, "1234567890-1600000000@g.us", "Five-a-side")
    message(store, 10, 1, "who's in for saturday?",
            from_jid="14155550188@s.whatsapp.net", push="Theo")

    assert whatsapp.poll(path=store) == 1
    stored = db.messages_since("2000-01-01", source="whatsapp")[0]
    assert stored.metadata["group"] is True
    assert stored.metadata["author_label"] == "Theo"


def test_polling_resumes_from_the_primary_key_not_the_clock(store):
    """Core Data hands out keys in insert order, so a key cursor survives a
    message arriving with an old date — which dates do not."""
    now = datetime.now(timezone.utc)
    chat(store, 1, "14155550142@s.whatsapp.net", "Dev Shah")
    message(store, 10, 1, "first", when=now)

    assert whatsapp.poll(path=store) == 1
    assert db.get_sync_state(whatsapp.CURSOR_KEY) == "10"

    assert whatsapp.poll(path=store) == 0, "nothing new"

    # A late arrival: a higher key carrying an older date.
    message(store, 11, 1, "sent yesterday, synced now", when=now - timedelta(days=1))
    assert whatsapp.poll(path=store) == 1, "the key cursor caught what a date filter would miss"
    assert db.get_sync_state(whatsapp.CURSOR_KEY) == "11"


def test_the_same_message_is_never_stored_twice(store):
    chat(store, 1, "14155550142@s.whatsapp.net", "Dev Shah")
    message(store, 10, 1, "hello")

    assert whatsapp.poll(path=store) == 1
    db.set_sync_state(whatsapp.CURSOR_KEY, "")     # force a full re-read
    assert whatsapp.poll(path=store) == 0
    assert len(db.messages_since("2000-01-01", source="whatsapp")) == 1


def test_whatsapps_own_notices_are_not_messages(store):
    chat(store, 1, "14155550142@s.whatsapp.net", "Dev Shah")
    message(store, 10, 1, "Messages and calls are end-to-end encrypted.")
    message(store, 11, 1, "<Media omitted>")
    message(store, 12, 1, "a real message")

    assert whatsapp.poll(path=store) == 1
    assert db.messages_since("2000-01-01", source="whatsapp")[0].text == "a real message"


def test_empty_messages_are_skipped(store):
    """Media-only rows carry no text and are not loose ends."""
    chat(store, 1, "14155550142@s.whatsapp.net", "Dev Shah")
    conn = sqlite3.connect(store)
    conn.execute("INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, "
                 "ZMESSAGEDATE, ZTEXT) VALUES (10, 1, 0, ?, NULL)",
                 (apple_time(datetime.now(timezone.utc)),))
    conn.commit()
    conn.close()

    assert whatsapp.poll(path=store) == 0
