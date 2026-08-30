"""Shared ingestion plumbing: identity resolution and thread bookkeeping."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .. import db
from ..models import Message, Person, Conversation, new_id, now_iso

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_handle(handle: str) -> str:
    """Phone numbers and emails arrive in a dozen shapes; flatten them."""
    h = (handle or "").strip().lower()
    if "@" in h:
        return h
    digits = re.sub(r"\D", "", h)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or h


def slugify(name: str) -> str:
    return _NON_ALNUM.sub("-", name.strip().lower()).strip("-") or "unknown"


class IdentityResolver:
    """Maps source-native handles onto Person rows (§3, Contacts)."""

    def __init__(
        self,
        people: Optional[Iterable[Person]] = None,
        handle_names: Optional[Dict[str, str]] = None,
    ):
        self._by_handle: Dict[str, Person] = {}
        self._by_id: Dict[str, Person] = {}
        # {normalised handle -> contact name}, e.g. from macOS Contacts.
        self._handle_names: Dict[str, str] = handle_names or {}
        for p in people if people is not None else db.list_people():
            self.add(p)

    def add(self, person: Person) -> Person:
        self._by_id[person.id] = person
        for h in person.handles:
            self._by_handle[normalise_handle(h)] = person
        self._by_handle.setdefault(normalise_handle(person.display_name), person)
        return person

    def resolve(self, handle: str, display_name: Optional[str] = None) -> Person:
        key = normalise_handle(handle)
        if key in self._by_handle:
            person = self._by_handle[key]
            # Upgrade a person still labelled with a raw number/handle (no
            # letters) once Contacts can name them.
            contact = self._handle_names.get(key)
            if contact and not any(ch.isalpha() for ch in person.display_name):
                person.display_name = contact
                db.upsert_person(person)
            return person
        if display_name:
            alt = normalise_handle(display_name)
            if alt in self._by_handle:
                person = self._by_handle[alt]
                if handle not in person.handles:
                    person.handles.append(handle)
                    db.upsert_person(person)
                    self._by_handle[key] = person
                return person
        # Unknown counterpart — prefer a real contact name over the raw handle
        # (a phone number/email), then create a provisional person.
        name = display_name or self._handle_names.get(key) or handle
        person = Person(id=slugify(name), display_name=name, relationship=None, handles=[handle])
        db.upsert_person(person)
        return self.add(person)

    def by_id(self, person_id: str) -> Optional[Person]:
        return self._by_id.get(person_id)


def load_people(path: Path) -> List[Person]:
    """Seed the people table from a Contacts export."""
    raw = json.loads(Path(path).read_text())
    people = []
    for entry in raw:
        person = Person(
            id=entry.get("id") or slugify(entry["display_name"]),
            display_name=entry["display_name"],
            relationship=entry.get("relationship"),
            handles=entry.get("handles", []),
            created_at=now_iso(),
        )
        db.upsert_person(person)
        people.append(person)
    return people


def ensure_conversation(conversation_id: str, source: str, display_name: str, is_group: bool = False) -> Conversation:
    thread = Conversation(id=conversation_id, source=source, display_name=display_name, is_group=is_group)
    return db.upsert_conversation(thread)


def stable_message_id(source: str, external_id: str) -> str:
    """Deterministic so a re-import of the same export can't duplicate rows."""
    digest = hashlib.sha1(f"{source}:{external_id}".encode()).hexdigest()
    return f"{source}-{digest[:24]}"


def make_message(
    source: str,
    conversation_id: str,
    external_id: str,
    timestamp: str,
    text: str,
    person_id: Optional[str],
    is_from_user: bool,
    metadata: Optional[dict] = None,
) -> Message:
    return Message(
        id=stable_message_id(source, external_id),
        source=source,
        conversation_id=conversation_id,
        external_id=external_id,
        person_id=person_id,
        is_from_user=is_from_user,
        timestamp=timestamp,
        text=text,
        metadata=metadata or {},
    )


__all__ = [
    "IdentityResolver",
    "ensure_conversation",
    "load_people",
    "make_message",
    "normalise_handle",
    "slugify",
    "stable_message_id",
]
