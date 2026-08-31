"""WhatsApp ingestion (§3, §9) — the live store, and the export fallback.

Two doors, like ``imessage.py``: ``poll()`` reads the desktop app's own
database, and ``import_export()`` parses an "Export chat" ``.txt`` for anyone
without the Mac app.

**The live store.** WhatsApp Desktop keeps every message in
``~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite``
— plain SQLite, Core Data table names, the same schema WhatsApp uses on iOS.
It sits in a group container rather than a TCC-protected path, so unlike
``chat.db`` this one needs no Full Disk Access at all. For someone whose main
channel is WhatsApp, this is the difference between the product working and
not: the first machine probed held 11,693 messages of text across 263 chats,
against three useful notifications from the same Mac.

The schema is WhatsApp's, undocumented, and free to change under us — so it
is probed rather than trusted, and an unreadable or unfamiliar store is a
logged no-op, never an exception. Same bargain as every other local store.

WhatsApp's export format varies by locale. Two dominant shapes are parsed:

    [7/18/26, 9:14:03 AM] Dev Shah: message text        (iOS, US)
    18/07/2026, 09:14 - Dev Shah: message text          (Android, EU)

Continuation lines (a message containing newlines) are appended to the
preceding entry rather than dropped.
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import db
from ..models import Message
from .base import (IdentityResolver, ensure_conversation, make_message,
                   normalise_handle, slugify)

log = logging.getLogger(__name__)

SOURCE = "whatsapp"

# The desktop app's own database. A group container, not a TCC-protected
# path: readable without Full Disk Access.
LIVE_STORE = (Path.home() / "Library" / "Group Containers"
              / "group.net.whatsapp.WhatsApp.shared" / "ChatStorage.sqlite")
CURSOR_KEY = "whatsapp:message_pk"

# Core Data dates: seconds since 2001-01-01 UTC.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
BACKFILL_DAYS = 30

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


# ------------------------------------------------------- the live store
def _jid_handle(jid: str) -> str:
    """The phone number out of a WhatsApp JID.

    JIDs look like ``4155550142@s.whatsapp.net`` for people and
    ``1234567890-1600000000@g.us`` for groups. Only the left half is an
    identity, and normalising it the way every other source does is what lets
    a WhatsApp thread and an iMessage thread resolve to one person.
    """
    left = (jid or "").split("@", 1)[0]
    left = left.split("-", 1)[0] if "-" in left else left
    return normalise_handle(left) if left else ""


def _is_group(jid: str) -> bool:
    return (jid or "").endswith("@g.us")


def _when(value) -> Optional[str]:
    if value is None:
        return None
    try:
        return (APPLE_EPOCH + timedelta(seconds=float(value))).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return None


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def read_store(path: Path, after_pk: Optional[int] = None,
               since: Optional[float] = None) -> List[Dict[str, object]]:
    """Every text message the store holds, oldest first.

    Ordered and resumed by ``Z_PK`` rather than by date: Core Data hands out
    primary keys in insert order, so a key cursor survives the clock going
    backwards and the late arrival of an old message — the lesson `imessage.py`
    already learned about trusting dates for sync order.

    Returns [] with the schema logged when the shape isn't what we expect.
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        message_cols = _columns(conn, "ZWAMESSAGE")
        if "ZTEXT" not in message_cols or "ZMESSAGEDATE" not in message_cols:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 40")]
            log.warning("whatsapp: unexpected schema; tables=%s ZWAMESSAGE cols=%s",
                        tables, message_cols[:20])
            return []

        has_session = bool(_columns(conn, "ZWACHATSESSION"))
        clauses = ["m.ZTEXT IS NOT NULL", "m.ZTEXT <> ''"]
        params: List[object] = []
        if after_pk is not None:
            clauses.append("m.Z_PK > ?")
            params.append(after_pk)
        elif since is not None:
            clauses.append("m.ZMESSAGEDATE > ?")
            params.append(since)

        query = f"""
            SELECT m.Z_PK AS pk, m.ZTEXT AS text, m.ZMESSAGEDATE AS when_at,
                   m.ZISFROMME AS from_me, m.ZFROMJID AS from_jid,
                   m.ZTOJID AS to_jid, m.ZPUSHNAME AS push_name,
                   {'s.ZCONTACTJID AS chat_jid, s.ZPARTNERNAME AS partner'
                    if has_session else "NULL AS chat_jid, NULL AS partner"}
            FROM ZWAMESSAGE m
            {'LEFT JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION' if has_session else ''}
            WHERE {' AND '.join(clauses)}
            ORDER BY m.Z_PK
        """
        rows = conn.execute(query, params).fetchall()
    except (sqlite3.Error, OSError) as exc:
        log.warning("whatsapp: unreadable store (%s: %s)", type(exc).__name__, exc)
        return []
    finally:
        if conn is not None:
            conn.close()

    found: List[Dict[str, object]] = []
    for row in rows:
        text = (row["text"] or "").strip()
        if not text or _is_system(text):
            continue
        chat_jid = row["chat_jid"] or row["to_jid"] or row["from_jid"] or ""
        found.append({
            "pk": row["pk"],
            "text": text,
            "when": _when(row["when_at"]),
            "from_me": bool(row["from_me"]),
            "chat_jid": chat_jid,
            "partner": (row["partner"] or "").strip(),
            "sender_jid": row["from_jid"] or "",
            "push_name": (row["push_name"] or "").strip(),
        })
    return found


def store_rows(rows: List[Dict[str, object]],
               resolver: Optional[IdentityResolver] = None) -> int:
    """Persist live-store rows as messages. Returns rows written."""
    resolver = resolver or IdentityResolver()
    messages: List[Message] = []
    for row in rows:
        chat_jid = str(row["chat_jid"])
        group = _is_group(chat_jid)
        # The chat's own JID names the thread, so a renamed contact doesn't
        # fork the conversation the way a slugified display name would.
        conversation_id = f"{SOURCE}:{chat_jid or 'unknown'}"
        title = str(row["partner"]) or _jid_handle(chat_jid) or "WhatsApp"
        ensure_conversation(conversation_id, SOURCE, title, is_group=group)

        person = None
        if not row["from_me"]:
            # In a group the sender is whoever sent it; one-to-one, the chat
            # partner is the sender. The handle is what ties this to their
            # iMessage and mail — the name is only a label.
            handle = _jid_handle(str(row["sender_jid"]) or chat_jid)
            name = str(row["push_name"]) or (str(row["partner"]) if not group else "")
            person = resolver.resolve(handle or None, name or None)

        messages.append(make_message(
            source=SOURCE,
            conversation_id=conversation_id,
            # The store's primary key is stable for the life of the database
            # and unique across chats.
            external_id=f"pk:{row['pk']}",
            timestamp=str(row["when"]) if row["when"] else None,
            text=str(row["text"]),
            person_id=person.id if person else None,
            is_from_user=bool(row["from_me"]),
            metadata={"chat": chat_jid, "group": group,
                      **({"author_label": str(row["push_name"])} if row["push_name"] else {})},
        ))
    return db.insert_messages(messages)


def poll(resolver: Optional[IdentityResolver] = None,
         path: Optional[Path] = None) -> int:
    """Ingest new WhatsApp messages from the desktop app's own database.

    Copy-then-read like `chat.db`: the store is WAL-mode and written while
    WhatsApp runs. Any failure is a logged no-op — the store belongs to
    WhatsApp, and being mid-write or newly-shaped is a thing it may do.
    """
    src = Path(path) if path else LIVE_STORE
    if not src.exists():
        return 0

    raw = db.get_sync_state(CURSOR_KEY)
    after_pk = int(raw) if raw and raw.isdigit() else None
    since = None if after_pk is not None else (
        datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS) - APPLE_EPOCH
    ).total_seconds()

    if resolver is None:
        # Contacts gives the phone numbers real names, exactly as it does for
        # iMessage — and it is what makes a WhatsApp thread join the rest of
        # a person rather than starting a stranger.
        from . import contacts
        resolver = IdentityResolver(handle_names=contacts.load_handle_names())

    try:
        try:
            rows = read_store(src, after_pk, since)
        except sqlite3.Error:
            with tempfile.TemporaryDirectory(prefix="lifeline-whatsapp-") as tmp:
                copy = Path(tmp) / "ChatStorage.sqlite"
                for suffix in ("", "-wal", "-shm"):
                    side = Path(str(src) + suffix)
                    if side.exists():
                        shutil.copy2(side, str(copy) + suffix)
                rows = read_store(copy, after_pk, since)
    except (OSError, sqlite3.Error) as exc:
        log.warning("whatsapp poll no-op: %s: %s", type(exc).__name__, exc)
        return 0

    written = store_rows(rows, resolver=resolver)
    if rows:
        db.set_sync_state(CURSOR_KEY, str(max(int(r["pk"]) for r in rows)))
    return written
