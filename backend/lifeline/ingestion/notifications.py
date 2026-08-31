"""Notification ingestion (§v3) — the apps we will never integrate.

Slack, WhatsApp, Signal, the bank, the delivery tracker: every one of them can
leave a loose end, and writing a connector for each is a life's work. macOS
keeps one store of what every app has told the user, behind the Full Disk
Access grant `chat.db` already needs, so one reader gets partial coverage of
all of them at once.

**A window, not a log.** This is the thing to hold in mind. Notification
Center keeps what is currently delivered plus recent history, and prunes when
the user clears it — so the store is a live buffer being sampled, not an
archive being read. Whatever a poll sees must be written down, because the
next poll may find it gone. A tidy user who swipes their notifications away
leaves us almost nothing, and that is a normal outcome, not a failure.

**What it can and cannot do.** A notification is whatever the app chose to
display: usually one line, often truncated, sometimes just "New message".
It is enough to *notice* a dropped thread and never enough to *work* one, so
these become messages the stack can raise — not evidence a move is built on.

**The schema is Apple's**, undocumented and free to drift: `app` maps ids to
bundle identifiers, `record` holds a delivery date and a binary plist of the
words. Probe it, log what is found, never raise — the same bargain
`applecal.py` and `applemail.py` strike with their own stores.
"""
from __future__ import annotations

import logging
import os
import plistlib
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .. import db
from ..models import Message, now_iso
from .base import IdentityResolver, ensure_conversation, make_message

log = logging.getLogger(__name__)

SOURCE = "notification"
STORE = (Path.home() / "Library" / "Group Containers"
         / "group.com.apple.usernoted" / "db2" / "db")
CURSOR_KEY = "notifications:delivered_through"

# Core Data / CF dates: seconds since 2001-01-01 UTC.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# How far back a first run reaches. The store rarely holds more than a few
# days anyway; this only bounds the very first sample.
BACKFILL_DAYS = 14

# The plist keys Apple uses for the words. Short, undocumented, stable for
# years — but looked up by walking rather than by a fixed path, because the
# nesting around them has moved between releases.
_TITLE_KEYS = ("titl",)
_SUBTITLE_KEYS = ("subt",)
_BODY_KEYS = ("body",)

# Our own notifications are not news to us.
_SELF = ("dev.clyon.looseends",)

# Apps whose notifications are never a loose end: the OS talking to itself,
# and the ambient noise of media and games. Matched on the bundle id.
_NOISE = re.compile(
    r"""
      ^com\.apple\.((Software)?Update|iTunes|Music|TV|Podcasts
                    |photolibraryd|ScreenTime|findmy|weather|Siri|clock|Passbook)
    | ^com\.(spotify|netflix|hulu|disney)\.
    | game
    """,
    re.I | re.X,
)


def _when(value: Any) -> Optional[str]:
    """An Apple-epoch float to an ISO timestamp, or None if it isn't one."""
    if value is None:
        return None
    try:
        return (APPLE_EPOCH + timedelta(seconds=float(value))).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return None


def _walk_for(node: Any, keys: Iterable[str]) -> str:
    """Find the first string under any of `keys`, at any depth.

    The words live a couple of dicts down inside the record's plist, and the
    exact nesting has changed across macOS releases. Searching for the key is
    stable in a way that hard-coding `plist["req"]["titl"]` is not.
    """
    wanted = set(keys)
    stack: List[Any] = [node]
    while stack:
        current = stack.pop(0)
        if isinstance(current, dict):
            for key, value in current.items():
                if key in wanted and isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return ""


def parse_record(blob: Any) -> Dict[str, str]:
    """The words out of one record's plist: title, subtitle, body.

    Never raises. A blob that isn't a plist, or is a plist shaped in a way
    this doesn't recognise, yields empty strings and is skipped upstream.
    """
    if not isinstance(blob, (bytes, bytearray)):
        return {"title": "", "subtitle": "", "body": ""}
    try:
        plist = plistlib.loads(bytes(blob))
    except Exception:
        return {"title": "", "subtitle": "", "body": ""}
    return {
        "title": _walk_for(plist, _TITLE_KEYS),
        "subtitle": _walk_for(plist, _SUBTITLE_KEYS),
        "body": _walk_for(plist, _BODY_KEYS),
    }


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def read_store(path: Path, since: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every notification the store still holds, newest last.

    Returns [] with the schema logged when the shape isn't what we expect —
    the first live run against a new macOS is the real probe.
    """
    # The connect is inside the try on purpose: without Full Disk Access
    # macOS refuses at open time with `DatabaseError: authorization denied`,
    # and this function promises its callers a list, never an exception.
    conn = None
    try:
        uri = f"file:{Path(path).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        record_cols = _columns(conn, "record")
        if "data" not in record_cols or "app_id" not in record_cols:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 40")]
            log.warning("notifications: unexpected schema; tables=%s record cols=%s",
                        tables, record_cols[:20])
            return []

        # Which column carries the delivery time varies; take the best present.
        when_col = next((c for c in ("delivered_date", "request_date", "request_last_date")
                         if c in record_cols), None)
        if when_col is None:
            log.warning("notifications: no date column in record: %s", record_cols[:20])
            return []

        has_app = bool(_columns(conn, "app"))
        query = (
            f"SELECT r.data AS data, r.{when_col} AS when_at, "
            f"{'a.identifier' if has_app else 'NULL'} AS bundle, "
            f"r.rec_id AS rec_id FROM record r "
            + ("LEFT JOIN app a ON a.app_id = r.app_id " if has_app else "")
            + (f"WHERE r.{when_col} > ? " if since is not None else "")
            + f"ORDER BY r.{when_col}"
        )
        rows = conn.execute(query, (since,) if since is not None else ()).fetchall()
    except (sqlite3.Error, OSError) as exc:
        log.warning("notifications: unreadable store (%s: %s)",
                    type(exc).__name__, exc)
        return []
    finally:
        if conn is not None:
            conn.close()

    found: List[Dict[str, Any]] = []
    for row in rows:
        words = parse_record(row["data"])
        if not words["title"] and not words["body"]:
            continue
        found.append({
            "bundle": (row["bundle"] or "unknown").strip(),
            "rec_id": row["rec_id"],
            "raw_when": row["when_at"],
            "when": _when(row["when_at"]),
            **words,
        })
    return found


def readable() -> bool:
    """Whether this process can open the store at all — the Full Disk Access
    question, asked without pretending an empty window means the same thing
    as a locked door."""
    if not STORE.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{STORE.resolve()}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        conn.close()
        return True
    except (sqlite3.Error, OSError):
        return False


def is_worth_keeping(item: Dict[str, Any]) -> bool:
    """Whether a notification could plausibly be somebody's loose end.

    Deliberately shallow: the extraction pipeline decides what is an item, and
    this only keeps the obvious noise out of the store — the OS talking to
    itself, media, games, and our own notifications coming back to us.
    """
    bundle = item.get("bundle") or ""
    if any(bundle.startswith(prefix) for prefix in _SELF):
        return False
    if _NOISE.search(bundle):
        return False
    # A notification with nothing but a title is usually a badge in words
    # ("3 new items"); one with a body is somebody saying something.
    return bool(item.get("body"))


def store(items: Iterable[Dict[str, Any]],
          resolver: Optional[IdentityResolver] = None) -> int:
    """Persist notifications as messages. Returns rows written.

    One conversation per app, because that is the only grouping a
    notification carries — there is no thread id to be had. The sender's name
    is usually the title ("Dev Shah"), so identity resolution gets a chance
    at it, and a hit means a Slack ping can join the same person's iMessage
    and mail rather than starting a stranger.
    """
    resolver = resolver or IdentityResolver()
    messages: List[Message] = []
    for item in items:
        bundle = item["bundle"]
        conversation_id = f"{SOURCE}:{bundle}"
        app_name = bundle.rsplit(".", 1)[-1] or bundle
        ensure_conversation(conversation_id, SOURCE, app_name, is_group=False)

        title, subtitle, body = item["title"], item["subtitle"], item["body"]
        # A title is a name far more often than it is a sentence, so it is
        # worth asking who it is — but never worth inventing a person for.
        person = resolver.resolve(None, title) if title else None
        headline = " · ".join(part for part in (title, subtitle) if part)
        text = f"{headline}\n\n{body}".strip() if headline else body

        messages.append(make_message(
            source=SOURCE,
            conversation_id=conversation_id,
            # The store's row id is reused as records are pruned and added, so
            # identity is the words and the moment, not the row.
            external_id=f"{bundle}:{item.get('when') or item.get('rec_id')}:{hash(text) & 0xffffffff:08x}",
            timestamp=item.get("when") or now_iso(),
            text=text,
            person_id=person.id if person else None,
            is_from_user=False,
            metadata={
                "app": bundle,
                "app_name": app_name,
                "title": title,
                "subtitle": subtitle,
                # Said plainly so every downstream reader knows the ceiling:
                # this is a glimpse, not a conversation.
                "glimpse": True,
            },
        ))
    return db.insert_messages(messages)


def seen_apps() -> List[Dict[str, Any]]:
    """Which apps have contributed, and how much — the answer to "what is it
    reading?", which this source has to be able to answer on demand."""
    rows = db.get_connection().execute(
        "SELECT json_extract(metadata, '$.app') AS app, COUNT(*) AS n, "
        "MAX(timestamp) AS latest FROM messages WHERE source = ? "
        "GROUP BY app ORDER BY n DESC", (SOURCE,),
    ).fetchall()
    return [dict(row) for row in rows if row["app"]]


def poll(resolver: Optional[IdentityResolver] = None,
         path: Optional[Path] = None) -> int:
    """Sample the notification store. Returns rows written.

    Copy-then-read like `chat.db`: the store is WAL-mode and written by
    `usernoted` while we look. Any failure is a no-op that says its name.
    """
    if os.environ.get("LIFELINE_NO_NOTIFICATIONS"):
        return 0
    src = Path(path) if path else STORE
    if not src.exists():
        return 0

    cursor = db.get_sync_state(CURSOR_KEY)
    try:
        since = float(cursor) if cursor else (
            datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS) - APPLE_EPOCH
        ).total_seconds()
    except ValueError:
        since = None

    try:
        try:
            found = read_store(src, since)
        except sqlite3.Error:
            with tempfile.TemporaryDirectory(prefix="lifeline-noted-") as tmp:
                copy = Path(tmp) / "db"
                for suffix in ("", "-wal", "-shm"):
                    side = Path(str(src) + suffix)
                    if side.exists():
                        shutil.copy2(side, str(copy) + suffix)
                found = read_store(copy, since)
    except (OSError, sqlite3.Error) as exc:
        log.warning("notifications poll no-op: %s: %s", type(exc).__name__, exc)
        return 0

    keepers = [item for item in found if is_worth_keeping(item)]
    written = store(keepers, resolver=resolver)

    high_water = max((float(i["raw_when"]) for i in found
                      if i.get("raw_when") is not None), default=None)
    if high_water is not None:
        db.set_sync_state(CURSOR_KEY, repr(high_water))
    log.info("notifications: %d in the window, %d kept, %d new",
             len(found), len(keepers), written)
    return written
