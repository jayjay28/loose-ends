"""Google Calendar ingestion (§3 — first-class source, not an afterthought).

Provides event/deadline context, RSVP status and birthdays to the ranking
engine, and "the event happened and wasn't cancelled" evidence to the
completion engine. Polling uses Google's syncToken for incremental reads.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional

import httpx

from .. import db
from ..models import CalendarEvent, now_iso

log = logging.getLogger(__name__)
from .google_auth import authed_client

API = "https://www.googleapis.com/calendar/v3"
SYNC_KEY_PREFIX = "calendar:sync_token:"
WINDOW_PAST_DAYS = 60
WINDOW_FUTURE_DAYS = 180


def _when(node: dict) -> Optional[str]:
    if not node:
        return None
    if node.get("dateTime"):
        return node["dateTime"]
    if node.get("date"):     # all-day event
        return f"{node['date']}T00:00:00+00:00"
    return None


def normalise(raw: dict, calendar_id: str = "primary") -> CalendarEvent:
    attendees = raw.get("attendees", []) or []
    self_response = None
    for a in attendees:
        if a.get("self"):
            self_response = a.get("responseStatus")
            break
    return CalendarEvent(
        id=raw["id"],
        calendar_id=calendar_id,
        summary=raw.get("summary", "(no title)"),
        description=raw.get("description", "") or "",
        location=raw.get("location", "") or "",
        start_at=_when(raw.get("start", {})),
        end_at=_when(raw.get("end", {})),
        status=raw.get("status", "confirmed"),
        attendees=[a.get("email", "") for a in attendees if a.get("email")],
        self_response=self_response,
        updated_at=raw.get("updated") or now_iso(),
    )


def store(events: Iterable[CalendarEvent]) -> int:
    return db.upsert_calendar_events(events)


# ---------------------------------------------------------------- .ics door
#
# §v2.8 phase 0.3. Invite attachments carry the same information the Calendar
# API serves — exact dates, exact titles, real UIDs — and they keep arriving
# while that API 403s on every poll. Parsed with no model in the loop.

_PARTSTAT = {
    "NEEDS-ACTION": "needsAction",
    "ACCEPTED": "accepted",
    "DECLINED": "declined",
    "TENTATIVE": "tentative",
}


def _ics_id(uid: str) -> str:
    """The UID, minus Google's suffix. A Google invite's UID is
    `{event_id}@google.com`, and the Calendar API serves the same event as
    `{event_id}` — stripping the suffix means the day that API works again,
    its rows land on these instead of doubling every meeting."""
    uid = (uid or "").strip()
    if uid.endswith("@google.com"):
        return uid[: -len("@google.com")]
    return uid or f"ics-{now_iso()}"


def _ics_when(value) -> Optional[str]:
    """A VEVENT date or datetime as the ISO the store speaks — UTC for a
    timed event, midnight-UTC for an all-day one, same shape as `_when`."""
    from datetime import date, datetime, timezone as tz

    if value is None:
        return None
    dt = getattr(value, "dt", value)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz.utc)     # floating time: honest default
        return dt.astimezone(tz.utc).isoformat(timespec="seconds")
    if isinstance(dt, date):
        return f"{dt.isoformat()}T00:00:00+00:00"
    return None


def import_ics(text: str, account_email: Optional[str] = None) -> int:
    """Every VEVENT in one .ics body, upserted into calendar_events.

    Keyed on UID so a revised invite replaces rather than duplicates, and a
    METHOD:CANCEL cancels the row it once created. Recurring events store
    their first occurrence — the RRULE is not expanded.

    Returns events written. Never raises: a malformed calendar is a warning
    and a zero, per the same law the attachment parsers follow.
    """
    import icalendar

    account = (account_email or db.get_sync_state("gmail:account") or "").lower()
    try:
        calendar = icalendar.Calendar.from_ical(text)
    except Exception as exc:
        log.warning("unparseable ics: %s", exc)
        return 0

    method = str(calendar.get("METHOD", "")).upper()
    events = []
    for component in calendar.walk("VEVENT"):
        uid = str(component.get("UID", ""))
        status = str(component.get("STATUS", "confirmed")).lower()
        if method == "CANCEL":
            status = "cancelled"

        attendees_raw = component.get("ATTENDEE")
        if attendees_raw is None:
            attendees_raw = []
        elif not isinstance(attendees_raw, list):
            attendees_raw = [attendees_raw]
        attendees, self_response = [], None
        for a in attendees_raw:
            email = str(a).replace("mailto:", "").strip().lower()
            if email:
                attendees.append(email)
            if account and email == account:
                partstat = str(getattr(a, "params", {}).get("PARTSTAT", "")).upper()
                self_response = _PARTSTAT.get(partstat)

        events.append(CalendarEvent(
            id=_ics_id(uid),
            calendar_id="ics",
            summary=str(component.get("SUMMARY", "(no title)")),
            description=str(component.get("DESCRIPTION", "") or ""),
            location=str(component.get("LOCATION", "") or ""),
            start_at=_ics_when(component.get("DTSTART")),
            end_at=_ics_when(component.get("DTEND")),
            status=status,
            attendees=attendees,
            self_response=self_response,
            updated_at=_ics_when(component.get("DTSTAMP")) or now_iso(),
        ))
    return db.upsert_calendar_events(events) if events else 0


def import_sample(path: Path) -> int:
    """Offline path for milestone testing."""
    payload = json.loads(Path(path).read_text())
    events = [
        CalendarEvent(
            id=e["id"],
            calendar_id=e.get("calendarId", "primary"),
            summary=e.get("summary", "(no title)"),
            description=e.get("description", ""),
            location=e.get("location", ""),
            start_at=e.get("start"),
            end_at=e.get("end"),
            status=e.get("status", "confirmed"),
            attendees=e.get("attendees", []),
            self_response=e.get("selfResponse"),
            updated_at=e.get("updated") or now_iso(),
        )
        for e in payload.get("events", [])
    ]
    return store(events)


# --------------------------------------------------------------- live API
def list_calendar_ids(client: httpx.Client) -> List[str]:
    response = client.get(f"{API}/users/me/calendarList")
    response.raise_for_status()
    return [c["id"] for c in response.json().get("items", []) if not c.get("deleted")]


def _poll_calendar(client: httpx.Client, calendar_id: str) -> int:
    sync_key = f"{SYNC_KEY_PREFIX}{calendar_id}"
    token = db.get_sync_state(sync_key)
    now = datetime.now(timezone.utc)

    params: dict = {"singleEvents": "true", "maxResults": 250, "showDeleted": "true"}
    if token:
        params["syncToken"] = token
    else:
        params["timeMin"] = (now - timedelta(days=WINDOW_PAST_DAYS)).isoformat(timespec="seconds")
        params["timeMax"] = (now + timedelta(days=WINDOW_FUTURE_DAYS)).isoformat(timespec="seconds")
        params["orderBy"] = "startTime"

    collected: List[CalendarEvent] = []
    next_sync: Optional[str] = None
    page: Optional[str] = None
    while True:
        if page:
            params["pageToken"] = page
        response = client.get(f"{API}/calendars/{calendar_id}/events", params=params)
        if response.status_code == 410:
            # Sync token expired — drop it and do a full window read.
            db.set_sync_state(sync_key, "")
            params.pop("syncToken", None)
            params["timeMin"] = (now - timedelta(days=WINDOW_PAST_DAYS)).isoformat(timespec="seconds")
            params["timeMax"] = (now + timedelta(days=WINDOW_FUTURE_DAYS)).isoformat(timespec="seconds")
            page = None
            continue
        response.raise_for_status()
        payload = response.json()
        collected.extend(normalise(item, calendar_id) for item in payload.get("items", []))
        next_sync = payload.get("nextSyncToken", next_sync)
        page = payload.get("nextPageToken")
        if not page:
            break

    stored = store(collected)
    if next_sync:
        db.set_sync_state(sync_key, next_sync)
    return stored


def poll() -> int:
    with authed_client() as client:
        total = 0
        for calendar_id in list_calendar_ids(client):
            total += _poll_calendar(client, calendar_id)
        return total
