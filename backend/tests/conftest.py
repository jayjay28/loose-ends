from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lifeline import db                                    # noqa: E402
from lifeline.config import Config, set_config             # noqa: E402
from lifeline.models import Entities, Item, Message, Person, Conversation, new_id, now_iso  # noqa: E402

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"
NOW = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFELINE_OFFLINE", "1")
    # No test may read a live store. This began as one line for chat.db after
    # the audit, and every reader added since has to join it: the suite once
    # pulled 19 real items out of the author's own WhatsApp database because
    # WhatsApp's container — unlike chat.db's — isn't behind Full Disk Access
    # and so nothing stopped it. Tests that exercise a reader pass an explicit
    # path to a fixture store.
    from lifeline.ingestion import applecal, applemail, notifications, whatsapp
    monkeypatch.setattr(applecal, "STORE", tmp_path / "no-such-Calendar.sqlitedb")
    monkeypatch.setattr(applemail, "MAIL_ROOT", tmp_path / "no-such-Mail")
    monkeypatch.setattr(whatsapp, "LIVE_STORE", tmp_path / "no-such-ChatStorage.sqlite")
    monkeypatch.setattr(notifications, "STORE", tmp_path / "no-such-noted-db")
    set_config(Config(db_path=tmp_path / "test.db", offline_extraction=True))
    db.reset_connection()
    db.get_connection()
    yield
    db.reset_connection()


@pytest.fixture
def sample_dir() -> Path:
    return SAMPLE_DIR


def make_person(person_id="tess", name="Tess", relationship="spouse", handles=None) -> Person:
    return db.upsert_person(Person(id=person_id, display_name=name, relationship=relationship, handles=handles or []))


def make_conversation(conversation_id="imessage:t1", source="imessage", name="Tess") -> Conversation:
    return db.upsert_conversation(Conversation(id=conversation_id, source=source, display_name=name))


def make_message(text, conversation_id="imessage:t1", person_id="tess", is_from_user=False, at=None, metadata=None, external_id=None, source=None) -> Message:
    message = Message(
        id=new_id(),
        source=source or conversation_id.split(":", 1)[0],
        conversation_id=conversation_id,
        external_id=external_id or new_id(),
        person_id=person_id,
        is_from_user=is_from_user,
        timestamp=(at or NOW).isoformat(timespec="seconds"),
        text=text,
        metadata=metadata or {},
    )
    db.insert_messages([message])
    return message


def make_item(
    item_type="promise",
    person_id="tess",
    person="Tess",
    text="can you call the vet",
    date=None,
    status="pending",
    at=None,
    conversation_id="imessage:t1",
    message_id=None,
) -> Item:
    item = Item(
        id=new_id(),
        source="imessage",
        conversation_id=conversation_id,
        message_id=message_id,
        person_id=person_id,
        person=person,
        timestamp=(at or NOW).isoformat(timespec="seconds"),
        type=item_type,
        raw_text=text,
        entities=Entities(item=None, date=date, link=None),
        suggested_action=f"Handle {text[:40]}",
        status=status,
        created_at=(at or NOW).isoformat(timespec="seconds"),
    )
    return db.save_item(item)


def days_from_now(n: float, base: datetime = NOW) -> str:
    return (base + timedelta(days=n)).isoformat(timespec="seconds")
