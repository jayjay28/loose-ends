"""iMessage ingestion.

There is no public read API on iOS (§3, §9), so two paths are supported:

  * ``import_export``  — the JSON produced by ``tools/export_imessage.py``,
    which the user runs periodically on their Mac. This is the MVP path.
  * ``import_chat_db`` — direct read of a ``chat.db`` copy, for when the
    export is generated on the same machine the backend runs on.

Both funnel into the same normalised Message rows.
"""
from __future__ import annotations

import json
import logging
import os
import select
import time
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import db
from ..models import Message
from .base import IdentityResolver, ensure_conversation, make_message

log = logging.getLogger(__name__)

SOURCE = "imessage"

# Apple stores timestamps as nanoseconds since 2001-01-01 UTC.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def apple_time_to_iso(value: Optional[int]) -> str:
    if not value:
        return APPLE_EPOCH.isoformat()
    seconds = value / 1_000_000_000 if value > 1_000_000_000_000 else value
    return (APPLE_EPOCH + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def import_export(path: Path, resolver: Optional[IdentityResolver] = None) -> int:
    """Import a Lifeline iMessage export bundle."""
    payload = json.loads(Path(path).read_text())
    resolver = resolver or IdentityResolver()
    account = {h.lower() for h in payload.get("account_handles", [])}

    messages: List[Message] = []
    for chat in payload.get("chats", []):
        conversation_id = f"{SOURCE}:{chat['chat_id']}"
        ensure_conversation(conversation_id, SOURCE, chat.get("display_name") or chat["chat_id"], bool(chat.get("is_group")))
        for raw in chat.get("messages", []):
            text = (raw.get("text") or "").strip()
            if not text:
                continue
            is_me = bool(raw.get("is_from_me")) or (raw.get("handle", "").lower() in account)
            person = None if is_me else resolver.resolve(raw.get("handle", ""), chat.get("display_name") if not chat.get("is_group") else None)
            messages.append(
                make_message(
                    source=SOURCE,
                    conversation_id=conversation_id,
                    external_id=raw["guid"],
                    timestamp=raw["date"],
                    text=text,
                    person_id=person.id if person else None,
                    is_from_user=is_me,
                    metadata={"service": raw.get("service", "iMessage")},
                )
            )
    return db.insert_messages(messages)


# macOS stopped populating `message.text` for most rows (nearly every row since
# mid-2026 on this machine carries the body only in `attributedBody`, and rows
# that once had `text` are rewritten without it later). The blob is an
# NSKeyedArchiver typedstream; the string sits after the "NSString" class
# marker, five bytes of type tags, and a length prefix.
_NSSTRING = b"NSString"
_NSSTRING_TAGS = 5
OBJECT_REPLACEMENT = "\ufffc"    # what Messages leaves in place of an attachment


def decode_attributed_body(blob: Optional[bytes]) -> Optional[str]:
    """The message body out of `attributedBody`, or None if none is there."""
    if not blob:
        return None
    i = blob.find(_NSSTRING)
    if i < 0:
        return None
    i += len(_NSSTRING) + _NSSTRING_TAGS
    if i >= len(blob):
        return None
    n = blob[i]
    i += 1
    if n == 0x81:
        n = int.from_bytes(blob[i:i + 2], "little")
        i += 2
    elif n == 0x82:
        n = int.from_bytes(blob[i:i + 4], "little")
        i += 4
    return blob[i:i + n].decode("utf-8", errors="replace")


def message_text(text: Optional[str], attributed_body: Optional[bytes]) -> str:
    """`text` when Messages kept it, else the decoded body; attachment
    placeholders stripped so an image-only message reads as empty."""
    body = text if text else decode_attributed_body(attributed_body)
    return (body or "").replace(OBJECT_REPLACEMENT, "").strip()


def import_chat_db(path: Path, resolver: Optional[IdentityResolver] = None,
                   since: Optional[str] = None,
                   after_rowid: Optional[int] = None) -> "Tuple[int, Optional[int]]":
    """Read a copy of ~/Library/Messages/chat.db directly.

    The live file is SIP-protected and locked while Messages runs; copy it
    first. Opened read-only so we can never write to the user's database.
    """
    resolver = resolver or IdentityResolver()
    uri = f"file:{Path(path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    query = """
        SELECT m.ROWID           AS rowid,
               m.guid            AS guid,
               m.text            AS text,
               m.attributedBody  AS attributed_body,
               m.date            AS date,
               m.is_from_me      AS is_from_me,
               h.id              AS handle,
               c.guid            AS chat_guid,
               c.display_name    AS chat_name,
               (SELECT COUNT(*) FROM chat_handle_join chj WHERE chj.chat_id = c.ROWID) AS participant_count,
               (SELECT h2.id FROM chat_handle_join chj2
                  JOIN handle h2 ON h2.ROWID = chj2.handle_id
                  WHERE chj2.chat_id = c.ROWID LIMIT 1) AS chat_handle
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c                ON c.ROWID = cmj.chat_id
        LEFT JOIN handle h         ON h.ROWID = m.handle_id
        WHERE ((m.text IS NOT NULL AND m.text != '') OR m.attributedBody IS NOT NULL
           OR m.cache_has_attachments = 1) {rowid_filter}
        ORDER BY m.date
    """.format(rowid_filter="AND m.ROWID > :after_rowid" if after_rowid is not None else "")
    attachment_query = """
        SELECT m.guid AS message_guid, a.filename AS path, a.transfer_name AS name,
               a.mime_type AS mime, a.total_bytes AS size, a.ROWID AS rowid
        FROM attachment a
        JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
        JOIN message m ON m.ROWID = maj.message_id
    """
    params = {"after_rowid": after_rowid} if after_rowid is not None else {}
    try:
        try:
            rows = conn.execute(query, params).fetchall()
        except sqlite3.OperationalError:
            # An older chat.db without cache_has_attachments — fall back to
            # the text-only filter; the attachment query below degrades too.
            rows = conn.execute(
                query.replace("\n           OR m.cache_has_attachments = 1", ""), params
            ).fetchall()
        attachments_by_guid: Dict[str, list] = {}
        try:
            for a in conn.execute(attachment_query):
                attachments_by_guid.setdefault(a["message_guid"], []).append(dict(a))
        except sqlite3.Error:
            attachments_by_guid = {}    # an old chat.db without the tables
    finally:
        conn.close()

    seen_conversations: Dict[str, bool] = {}
    messages: List[Message] = []
    max_rowid: Optional[int] = None
    for row in rows:
        try:
            max_rowid = max(max_rowid or 0, int(row["rowid"]))
        except (KeyError, TypeError, ValueError):
            pass
        ts = apple_time_to_iso(row["date"])
        if since and ts < since:
            continue
        text = message_text(row["text"], row["attributed_body"])
        carried = attachments_by_guid.get(row["guid"], [])
        if not text and not _carries_document(carried):
            # A photo with no caption stays out, as it always has — but a PDF
            # someone texted over is a document arriving by iMessage, and
            # dropping the message dropped the document with it.
            continue
        conversation_id = f"{SOURCE}:{row['chat_guid']}"
        is_group = (row["participant_count"] or 1) > 1
        display = row["chat_name"] or row["handle"] or row["chat_guid"]
        if conversation_id not in seen_conversations:
            ensure_conversation(conversation_id, SOURCE, display, is_group)
            seen_conversations[conversation_id] = True
        is_me = bool(row["is_from_me"])
        # Incoming: the message's own handle is the sender. Outgoing: the message
        # carries no handle, so for a 1:1 chat attribute it to the chat's sole
        # participant — the recipient — so "you replied to X" links to X (and can
        # close X's open loops, even ones that arrived on another channel).
        counterparty = row["chat_handle"] if is_me and not is_group else row["handle"]
        person = resolver.resolve(counterparty) if counterparty else None
        messages.append(
            make_message(
                source=SOURCE,
                conversation_id=conversation_id,
                external_id=row["guid"],
                timestamp=ts,
                text=text,
                person_id=person.id if person else None,
                is_from_user=is_me,
            )
        )
    inserted = db.insert_messages(messages)

    # §v2.8 phase 0.5 — read what those messages carried. The files live under
    # ~/Library/Messages/Attachments on the real disk regardless of whether we
    # just read a copy of the db, and a failure here must cost nothing but a
    # log line: the messages are already stored.
    try:
        _ingest_attachments(messages, attachments_by_guid)
    except Exception:
        log.exception("imessage attachment ingest failed; messages kept")
    return inserted, max_rowid


# The same shapes the gmail backfill treats as documents: cargo someone kept
# on purpose, whether or not text comes out of it today.
_DOCUMENT_SUFFIXES = (".pdf", ".ics", ".csv", ".txt", ".vcf",
                      ".doc", ".docx", ".xls", ".xlsx")


def _carries_document(metas: list) -> bool:
    for meta in metas or []:
        mime = (meta.get("mime") or "").lower()
        name = (meta.get("name") or meta.get("path") or "").lower()
        if mime.startswith(("application/pdf", "text/")) or name.endswith(_DOCUMENT_SUFFIXES):
            return True
    return False


def _ingest_attachments(messages: List[Message], attachments_by_guid: Dict[str, list]) -> int:
    """Attachment rows for every stored message that carried files.

    Metadata for everything — a photo is recorded as the OCR inventory, not
    parsed — and text out of the document types, by the same parsers, caps
    and never-raise rules Gmail's cargo goes through
    (`ingestion/attachments.py`).
    """
    import hashlib

    from ..models import Attachment, now_iso
    from . import attachments as attachments_mod

    inserted = 0
    for message in messages:
        for meta in attachments_by_guid.get(message.external_id, []):
            stored = db.get_message_by_external_id(SOURCE, message.external_id)
            if stored is None:
                continue
            filename = meta.get("name") or Path(meta.get("path") or "").name
            mime = meta.get("mime") or ""
            size = int(meta.get("size") or 0)
            path = Path(meta.get("path") or "").expanduser()

            if size > attachments_mod.MAX_FETCH_BYTES:
                inserted += db.insert_attachment(Attachment(
                    message_id=stored.id, source=SOURCE, remote_id=str(meta.get("rowid") or ""),
                    filename=filename, mime=mime, size_bytes=size,
                    sha256=f"unfetched:{meta.get('rowid') or filename}",
                    parsed_at=now_iso(), error=f"skipped: {size} bytes",
                ))
                continue

            try:
                data = path.read_bytes()
            except OSError as exc:
                # Not on this disk (iCloud-offloaded, or deleted). Transient
                # by nature — leave no row and let a later pass retry.
                log.info("attachment unreadable %s: %s", path, exc)
                continue

            sha = hashlib.sha256(data).hexdigest()
            known = db.attachment_text_by_sha(sha)
            if known is not None:
                text, error = known, None
            else:
                text, error = attachments_mod.parse(mime, filename, data)

            if text and attachments_mod._is_ics(mime, filename):
                try:
                    from . import gcal
                    gcal.import_ics(text)
                except Exception:
                    log.exception("ics import failed for %s", filename)

            inserted += db.insert_attachment(Attachment(
                message_id=stored.id, source=SOURCE, remote_id=str(meta.get("rowid") or ""),
                filename=filename, mime=mime, size_bytes=size or len(data),
                sha256=sha, text=text, parsed_at=now_iso(), error=error,
            ))
    return inserted


# --------------------------------------------------------------------------
# Live polling — the continuous source the poller calls each cycle.
# --------------------------------------------------------------------------

LIVE_CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
SYNC_KEY = "imessage:since"
# The checkpoint that actually filters. ROWID is monotonic in *sync order* —
# the order rows reach this Mac — where a message's date is the moment it was
# sent. The old date-keyed checkpoint skipped anything that synced in late:
# a reply received while the laptop was shut arrived in chat.db days after
# its date, landed behind the cursor, and was never ingested. That is audit
# finding #8, and it happened twice in one week (Theo B's replies reached
# the store five days after he sent them, via a manual rewind).
ROWID_KEY = "imessage:rowid"
BACKFILL_DAYS = 90


def poll(resolver: Optional[IdentityResolver] = None, db_path: Optional[Path] = None) -> int:
    """Ingest new iMessages since the last cycle.

    Reads the live ``~/Library/Messages/chat.db``. That file is WAL-mode and
    locked while Messages runs, so we copy the db + its ``-wal``/``-shm``
    sidecars to a temp dir and read the copy read-only — never touching the
    user's database. Requires the backend process to have Full Disk Access.

    First run backfills ``BACKFILL_DAYS``; after that it resumes from the
    checkpoint. De-duplication is by message GUID in ``insert_messages``, so a
    small re-read overlap between cycles is harmless.
    """
    src = Path(db_path) if db_path else LIVE_CHAT_DB
    if not src.exists():
        return 0

    # Seed the resolver with macOS Contacts so handles show as real names.
    if resolver is None:
        from . import contacts
        resolver = IdentityResolver(handle_names=contacts.load_handle_names())

    raw_rowid = db.get_sync_state(ROWID_KEY)
    after_rowid: Optional[int] = int(raw_rowid) if raw_rowid else None
    # Date filter only on the very first run (the backfill window) — or when a
    # rewound SYNC_KEY asks for a deliberate re-read. Once a ROWID cursor
    # exists it is the only filter, because dates lie about sync order.
    since = None if after_rowid is not None else (
        db.get_sync_state(SYNC_KEY) or
        (datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)).isoformat()
    )

    # Skip the copy entirely if nothing has been written since we last looked.
    last_poll = db.get_sync_state(SYNC_KEY)
    try:
        mtime = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return 0
    if after_rowid is not None and last_poll and mtime < last_poll:
        return 0

    started = datetime.now(timezone.utc).isoformat()
    try:
        inserted, max_rowid = _read(src, resolver, since, after_rowid)
    except (OSError, sqlite3.Error) as exc:
        # No Full Disk Access, or the DB is momentarily locked — treat as a
        # no-op this cycle rather than failing the whole poll. Named in the
        # log, because a silent no-op here once looked exactly like a quiet
        # week (124 cycles of it, 2026-08-22 to 08-27).
        log.warning("imessage poll no-op: %s: %s", type(exc).__name__, exc)
        return 0

    db.set_sync_state(SYNC_KEY, started)
    if max_rowid is not None:
        db.set_sync_state(ROWID_KEY, str(max_rowid))
    return inserted


def _read(src: Path, resolver: IdentityResolver, since: Optional[str],
          after_rowid: Optional[int] = None) -> "Tuple[int, Optional[int]]":
    """Read the live database, preferring not to copy half a gigabyte.

    This used to copy `chat.db` and its `-wal`/`-shm` sidecars into a temp dir
    on every cycle that saw a change. That was safe and it was expensive: a
    real database here is **449 MB**, so a chatty afternoon meant copying it
    again and again, and the cost grew with the user's history rather than
    with how much had actually been said.

    SQLite opens it read-only in place instead. WAL readers normally need to
    write the `-shm` to take a read lock, which is exactly why the copy existed
    — but a process with Full Disk Access can, and a read-only connection still
    cannot touch the user's data. The copy stays as the fallback, because the
    failure it guards against (a locked or unwritable `-shm`) is real and the
    only honest way to find out is to try.
    """
    try:
        return import_chat_db(src, resolver, since=since, after_rowid=after_rowid)
    except sqlite3.Error as exc:
        log.info("reading chat.db in place failed (%s) — falling back to a copy", exc)

    with tempfile.TemporaryDirectory(prefix="lifeline-chatdb-") as tmp:
        copy = Path(tmp) / "chat.db"
        for suffix in ("", "-wal", "-shm"):
            side = Path(str(src) + suffix)
            if side.exists():
                shutil.copy2(side, str(copy) + suffix)
        return import_chat_db(copy, resolver, since=since, after_rowid=after_rowid)


# --------------------------------------------------------------- watching
#
# A five-minute poll means a message can sit unseen for five minutes, and the
# thread that would have been opened by it doesn't exist until the next cycle.
# The database and its WAL are files, and macOS will say when they change, so
# there is no reason to keep asking.
#
# kqueue rather than a watch library: it is in the standard library, it is the
# native mechanism on the only platform where `chat.db` exists at all, and it
# adds nothing to install on the machine that runs the poller.

# `select` exposes the kqueue constants only where kqueue exists.
HAS_WATCH = hasattr(select, "kqueue")

_WATCH_FLAGS = 0
if HAS_WATCH:
    _WATCH_FLAGS = (
        select.KQ_NOTE_WRITE       # a message was written
        | select.KQ_NOTE_EXTEND    # the WAL grew
        | select.KQ_NOTE_DELETE    # checkpointed away
        | select.KQ_NOTE_RENAME    # or rotated
    )


def wait_for_change(timeout: float, db_path: Optional[Path] = None) -> bool:
    """Block until the Messages database is written to, or `timeout` elapses.

    Returns True when something changed. False on timeout, on a platform
    without kqueue, or when the files cannot be opened — every one of which
    should degrade to the interval poll rather than raise, because this runs
    in a background task whose failure the user would never see.

    Both `chat.db` and `chat.db-wal` are watched. In WAL mode almost every
    write lands in the sidecar and the main file only moves on checkpoint, so
    watching the database alone would miss most of what we care about and
    notice it minutes later, which is the problem this is meant to solve.
    """
    src = Path(db_path) if db_path else LIVE_CHAT_DB
    if not HAS_WATCH:
        return False

    fds: List[int] = []
    try:
        for suffix in ("", "-wal"):
            path = Path(str(src) + suffix)
            try:
                fds.append(os.open(path, os.O_RDONLY))
            except OSError:
                continue          # not there yet, or no Full Disk Access
        if not fds:
            # No Full Disk Access (or no files yet). Returning instantly here
            # made `watch_imessage`'s retry loop a busy-wait — 100% of a core
            # for as long as the grant was missing. Failure has to cost the
            # same time a quiet minute does.
            time.sleep(timeout)
            return False

        kq = select.kqueue()
        try:
            events = [
                select.kevent(
                    fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=_WATCH_FLAGS,
                )
                for fd in fds
            ]
            return bool(kq.control(events, 1, timeout))
        finally:
            kq.close()
    except OSError:
        time.sleep(timeout)   # same reasoning as the empty-fds path above
        return False
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
