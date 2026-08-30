"""Apple Calendar ingestion (§v2.8) — the calendar through the door that works.

The Google Calendar API has returned 403 on every poll for two weeks. But the
user's Google calendar syncs to this Mac through the OS itself, into
``~/Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb`` —
the same arrangement chat.db taught us to use: read the local store the OS
already maintains, copy-then-read-only, and treat the undocumented schema as
weather (probe it, log what you find, never raise).

Three doors now feed ``calendar_events``, and they must converge rather than
triple every meeting:

* the Google API (``gcal.py``) writes the event id
* invite attachments (``gcal.import_ics``) write the UID minus ``@google.com``
* this reader writes whatever external identifier the row carries, normalised
  the same way — so a Google-synced event lands on the same id through all
  three. Rows with no recognisable external id get ``applecal:<uuid>`` and a
  sameness guard (same start, same title, different door → skip).
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .. import db
from ..models import CalendarEvent, now_iso

log = logging.getLogger(__name__)

STORE = Path.home() / "Library" / "Group Containers" / "group.com.apple.calendar" / "Calendar.sqlitedb"

# Core Data dates: seconds since 2001-01-01 UTC (floats).
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# How much calendar is worth carrying: recent past for completion evidence,
# a year ahead for deadlines.
PAST_DAYS = 30
FUTURE_DAYS = 365


def _when(value) -> Optional[str]:
    if value is None:
        return None
    try:
        return (APPLE_EPOCH + timedelta(seconds=float(value))).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return None


def _converged_id(external_id: Optional[str], uuid: Optional[str]) -> str:
    """The same event through any door lands on one row. Google-synced items
    carry an external id shaped like the iCal UID; strip the suffix exactly
    as `gcal._ics_id` does. Anything unrecognisable gets its own namespace."""
    ext = (external_id or "").strip()
    if ext.endswith("@google.com"):
        return ext[: -len("@google.com")]
    if ext:
        return ext
    return f"applecal:{uuid or now_iso()}"


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def read_store(path: Path) -> List[CalendarEvent]:
    """Every event in the window, or [] with the schema logged when the
    store's shape isn't what we expect — the schema is Apple's, undocumented,
    and allowed to drift under us. The first live run is the real probe."""
    uri = f"file:{Path(path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        item_cols = _columns(conn, "CalendarItem")
        if "summary" not in item_cols or "start_date" not in item_cols:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 40"
            )]
            log.warning("applecal: unexpected schema; tables=%s CalendarItem cols=%s",
                        names, item_cols[:20])
            return []

        # Optional columns vary across macOS versions; select what exists.
        def col(name: str, fallback: str = "NULL") -> str:
            # Qualified with the item alias: the real store's Calendar table
            # carries external_id too, and the bare name is ambiguous — the
            # first live probe said exactly that.
            return f"i.{name} AS {name}" if name in item_cols else f"{fallback} AS {name}"

        window_lo = (datetime.now(timezone.utc) - timedelta(days=PAST_DAYS)
                     - APPLE_EPOCH).total_seconds()
        window_hi = (datetime.now(timezone.utc) + timedelta(days=FUTURE_DAYS)
                     - APPLE_EPOCH).total_seconds()

        has_calendar = "Calendar" in [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='Calendar'")]
        has_location = "Location" in [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='Location'")]

        query = f"""
            SELECT i.summary, i.start_date, i.end_date,
                   {col('all_day')}, {col('status', '0')},
                   {col('external_id', "''")}, {col('UUID', "''")},
                   {col('description', "''")}, {col('location_id')},
                   {col('hidden', '0')}
                   {', c.title AS calendar_title' if has_calendar else ", '' AS calendar_title"}
                   {', l.title AS location_title' if has_location else ", '' AS location_title"}
            FROM CalendarItem i
            {'LEFT JOIN Calendar c ON c.ROWID = i.calendar_id' if has_calendar and 'calendar_id' in item_cols else ''}
            {'LEFT JOIN Location l ON l.ROWID = i.location_id' if has_location and 'location_id' in item_cols else ''}
            WHERE i.start_date BETWEEN ? AND ?
        """
        rows = conn.execute(query, (window_lo, window_hi)).fetchall()
    finally:
        conn.close()

    events: List[CalendarEvent] = []
    for row in rows:
        summary = (row["summary"] or "").strip()
        if not summary or row["hidden"]:
            continue
        # CalendarItem.status: 3 is cancelled in every version observed.
        status = "cancelled" if row["status"] == 3 else "confirmed"
        events.append(CalendarEvent(
            id=_converged_id(row["external_id"], row["UUID"]),
            calendar_id=f"applecal:{row['calendar_title'] or 'local'}",
            summary=summary,
            description=(row["description"] or "")[:2000],
            location=row["location_title"] or "",
            start_at=_when(row["start_date"]),
            end_at=_when(row["end_date"]),
            status=status,
        ))
    return events


def poll(db_path: Optional[Path] = None) -> int:
    """Ingest the local calendar store. Returns events written.

    Copy-then-read like chat.db: the store is WAL-mode and actively written
    by calendard. Any failure is a no-op that says its name — the lesson the
    iMessage door spent five silent days teaching.
    """
    src = Path(db_path) if db_path else STORE
    if not src.exists():
        return 0
    try:
        try:
            events = read_store(src)
        except sqlite3.Error:
            with tempfile.TemporaryDirectory(prefix="lifeline-applecal-") as tmp:
                copy = Path(tmp) / "Calendar.sqlitedb"
                for suffix in ("", "-wal", "-shm"):
                    side = Path(str(src) + suffix)
                    if side.exists():
                        shutil.copy2(side, str(copy) + suffix)
                events = read_store(copy)
    except (OSError, sqlite3.Error) as exc:
        log.warning("applecal poll no-op: %s: %s", type(exc).__name__, exc)
        return 0

    written = 0
    for event in events:
        if _same_event_other_door(event):
            continue
        written += 1
        db.upsert_calendar_events([event])
    return written


def _same_event_other_door(event: CalendarEvent) -> bool:
    """An event with the same start and title already written by another door
    is the same meeting, and a second row for it is noise.

    Applies to every applecal write, converged id or not — the first live run
    added a twelfth "Bulky Items, Garbage" to eleven the API door had already
    written per-occurrence, because this guard only covered rows without an
    external id."""
    row = db.get_connection().execute(
        "SELECT 1 FROM calendar_events WHERE start_at = ? AND summary = ? "
        "AND id != ? LIMIT 1",
        (event.start_at, event.summary, event.id),
    ).fetchone()
    return row is not None
