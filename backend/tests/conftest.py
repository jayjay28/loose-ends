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
    # The live Apple Calendar store must never be read by a test — the same
    # law the audit wrote for chat.db. Tests that want the reader pass db_path.
    from lifeline.ingestion import applecal
    monkeypatch.setattr(applecal, "STORE", tmp_path / "no-such-Calendar.sqlitedb")
    set_config(Config(db_path=tmp_path / "test.db", offline_extraction=True))
    db.reset_connection()
    db.get_connection()
    yield
    db.reset_connection()


@pytest.fixture
def sample_dir() -> Path:
    return SAMPLE_DIR


def make_person(person_id="maya", name="Maya", relationship="spouse", handles=None) -> Person:
    return db.upsert_person(Person(id=person_id, display_name=name, relationship=relationship, handles=handles or []))


def make_conversation(conversation_id="imessage:t1", source="imessage", name="Maya") -> Conversation:
    return db.upsert_conversation(Conversation(id=conversation_id, source=source, display_name=name))


def make_message(text, conversation_id="imessage:t1", person_id="maya", is_from_user=False, at=None, metadata=None, external_id=None, source=None) -> Message:
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
    person_id="maya",
    person="Maya",
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
