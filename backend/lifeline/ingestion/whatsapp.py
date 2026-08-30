"""WhatsApp ingestion from "Export chat" ``.txt`` files (§3, §9).

WhatsApp's export format varies by locale. Two dominant shapes are parsed:

    [7/18/26, 9:14:03 AM] Dev Shah: message text        (iOS, US)
    18/07/2026, 09:14 - Dev Shah: message text          (Android, EU)

Continuation lines (a message containing newlines) are appended to the
preceding entry rather than dropped.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .. import db
from ..models import Message
from .base import IdentityResolver, ensure_conversation, make_message, slugify

SOURCE = "whatsapp"

_BRACKET = re.compile(
    r"^\[(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),\s*(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)\]\s*"
    r"(?P<author>[^:]{1,60}?):\s(?P<text>.*)$"
)
_DASH = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),\s*(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)\s*-\s*"
    r"(?P<author>[^:]{1,60}?):\s(?P<text>.*)$"
)

# Lines WhatsApp itself writes into the transcript.
_SYSTEM = (
    "messages and calls are end-to-end encrypted",
    "you deleted this message",
    "this message was deleted",
    "<media omitted>",
    "image omitted",
    "video omitted",
    "missed voice call",
    "missed video call",
)

_SELF_LABELS = {"you", "me"}


def _parse_datetime(date_s: str, time_s: str, day_first: bool) -> datetime:
    a, b, y = (int(x) for x in date_s.split("/"))
    day, month = (a, b) if day_first else (b, a)
    if y < 100:
        y += 2000
    time_s = time_s.strip().upper()
    meridiem = None
    if time_s.endswith("AM") or time_s.endswith("PM"):
        meridiem = time_s[-2:]
        time_s = time_s[:-2].strip()
    parts = [int(p) for p in time_s.split(":")]
    hour, minute = parts[0], parts[1]
    second = parts[2] if len(parts) > 2 else 0
    if meridiem == "PM" and hour != 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0
    return datetime(y, month, day, hour, minute, second, tzinfo=timezone.utc)


def _looks_day_first(lines: List[str]) -> bool:
    """If any first component exceeds 12 the export must be day-first."""
    for line in lines:
        m = _BRACKET.match(line) or _DASH.match(line)
        if m:
            first = int(m.group("date").split("/")[0])
            if first > 12:
                return True
            second = int(m.group("date").split("/")[1])
            if second > 12:
                return False
    return False


def _is_system(text: str) -> bool:
    low = text.strip().lower()
    return any(marker in low for marker in _SYSTEM)


def parse_export(path: Path) -> Tuple[List[Tuple[datetime, str, str]], bool]:
    lines = Path(path).read_text(errors="replace").splitlines()
    day_first = _looks_day_first(lines)
    entries: List[Tuple[datetime, str, str]] = []
    for raw in lines:
        line = raw.replace("‎", "").replace(" ", " ").rstrip()
        if not line:
            continue
        m = _BRACKET.match(line) or _DASH.match(line)
        if m:
            ts = _parse_datetime(m.group("date"), m.group("time"), day_first)
            entries.append((ts, m.group("author").strip(), m.group("text").strip()))
        elif entries:
            ts, author, text = entries[-1]
            entries[-1] = (ts, author, f"{text}\n{line.strip()}")
    return entries, day_first


def import_export(
    path: Path,
    contact_name: Optional[str] = None,
    is_group: bool = False,
    resolver: Optional[IdentityResolver] = None,
) -> int:
    resolver = resolver or IdentityResolver()
    entries, _ = parse_export(path)
    if not entries:
        return 0

    authors = {a for _, a, _ in entries if a.lower() not in _SELF_LABELS}
    if contact_name is None:
        contact_name = sorted(authors)[0] if authors else Path(path).stem
    thread_key = slugify(contact_name)
    conversation_id = f"{SOURCE}:{thread_key}"
    ensure_conversation(conversation_id, SOURCE, contact_name, is_group or len(authors) > 1)

    messages: List[Message] = []
    for index, (ts, author, text) in enumerate(entries):
        if _is_system(text) or not text.strip():
            continue
        is_me = author.lower() in _SELF_LABELS
        person = None if is_me else resolver.resolve(author, author)
        stamp = ts.isoformat(timespec="seconds")
        messages.append(
            make_message(
                source=SOURCE,
                conversation_id=conversation_id,
                # Timestamps repeat within a chat, so the index disambiguates.
                external_id=f"{thread_key}:{index}:{stamp}",
                timestamp=stamp,
                text=text,
                person_id=person.id if person else None,
                is_from_user=is_me,
                metadata={"author_label": author},
            )
        )
    return db.insert_messages(messages)
