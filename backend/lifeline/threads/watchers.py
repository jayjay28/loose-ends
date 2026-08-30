"""Standing monitors (§v2 step 6) — what makes the system proactive.

"Is the flight delayed? Is there traffic at SJU?" is not a question the user
asked. It is a **monitor the thread implied**, and the difference between those
two is the difference between a system that answers and one that watches.

Three things make this cheap enough to run constantly:

**A watcher does no thinking.** It runs on a cadence, checks local data with
SQL, and when something matches it *attaches evidence to the thread*. That's
all. The worker loop is already evidence-triggered (step 4), so it wakes up on
its next pass and does the interpreting. One LLM call happens, at the point
where judgment is actually needed, rather than one per check per watcher.

**There is no web here.** Tier 3 is step 8. Mostly that isn't the limitation it
sounds like: flight changes, bill amounts, delivery updates, renewal warnings
and school notices all arrive in the inbox already, so watching for them is a
parsing problem over data we have rather than a web problem. The web is for
what *isn't* in the inbox.

**A watcher expires.** "Every 3h until departure" has an end, and a monitor
that outlives its reason is just a scheduled way to waste money. Anything with
an `until` retires itself; anything whose thread closes goes with it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .. import db
from ..models import Evidence, ThreadState, new_id, now_iso, parse_iso

log = logging.getLogger(__name__)


class WatchKind:
    MAIL = "mail"            # new email matching a sender / label / phrase
    MESSAGES = "messages"    # new messages from a person or about a topic
    CALENDAR = "calendar"    # a matching calendar event appears or moves
    DEADLINE = "deadline"    # the thread's own date comes within N days

    ALL = (MAIL, MESSAGES, CALENDAR, DEADLINE)


# A floor on how often any watcher may run, whatever cadence it was given. The
# poller ticks every 5 minutes and nothing local changes faster than that in a
# way worth waking for.
MIN_CADENCE_MINUTES = 15


@dataclass
class Watcher:
    id: str = field(default_factory=new_id)
    thread_id: str = ""
    kind: str = WatchKind.MAIL
    spec: Dict[str, Any] = field(default_factory=dict)
    what: str = ""
    cadence_minutes: int = 180
    until: Optional[str] = None
    state: str = "active"
    last_checked_at: Optional[str] = None
    last_fired_at: Optional[str] = None
    fire_count: int = 0
    created_by: str = "worker"
    created_at: str = field(default_factory=now_iso)

    def to_row(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["spec"] = json.dumps(self.spec)
        return d

    @classmethod
    def from_row(cls, row: Any) -> "Watcher":
        d = dict(row)
        d["spec"] = json.loads(d.get("spec") or "{}")
        return cls(**d)


# ------------------------------------------------------------------ store
def add(
    thread_id: str,
    kind: str,
    what: str,
    spec: Optional[Dict[str, Any]] = None,
    cadence_minutes: int = 180,
    until: Optional[str] = None,
    created_by: str = "worker",
) -> Watcher:
    from . import ThreadError, _require

    _require(thread_id)
    if kind not in WatchKind.ALL:
        raise ThreadError(f"watcher kind must be one of {WatchKind.ALL}")

    watcher = Watcher(
        thread_id=thread_id,
        kind=kind,
        spec=spec or {},
        what=(what or "").strip()[:200],
        cadence_minutes=max(MIN_CADENCE_MINUTES, int(cadence_minutes)),
        until=until,
        created_by=created_by,
    )
    # The worker re-derives a thread's monitors every pass, so without this it
    # would stack up duplicates of the same watch.
    for existing in for_thread(thread_id):
        if existing.kind == kind and existing.spec == watcher.spec:
            return existing
    db.save_watcher(watcher)
    return watcher


def for_thread(thread_id: str, include_expired: bool = False) -> List[Watcher]:
    return db.thread_watchers(thread_id, include_expired=include_expired)


def remove(watcher_id: str) -> bool:
    return db.delete_watcher(watcher_id)


# ------------------------------------------------------------------ sweep
def due(reference: Optional[datetime] = None) -> List[Watcher]:
    now = reference or datetime.now(timezone.utc)
    out = []
    for watcher in db.active_watchers():
        checked = parse_iso(watcher.last_checked_at)
        if checked and checked + timedelta(minutes=watcher.cadence_minutes) > now:
            continue
        out.append(watcher)
    return out


def check(watcher: Watcher, reference: Optional[datetime] = None) -> List[Dict[str, str]]:
    """Run one watcher. Returns the evidence refs it found, and attaches them.

    Deliberately deterministic — a watcher that needed a model to decide
    whether something happened would cost a call every cadence tick per thread,
    which is the arithmetic that made the worker's cadence a decision in the
    first place.
    """
    now = reference or datetime.now(timezone.utc)
    since = watcher.last_checked_at or watcher.created_at
    found: List[Dict[str, str]] = []

    if watcher.kind == WatchKind.MAIL:
        found = _check_mail(watcher, since)
    elif watcher.kind == WatchKind.MESSAGES:
        found = _check_messages(watcher, since)
    elif watcher.kind == WatchKind.CALENDAR:
        found = _check_calendar(watcher, since)
    elif watcher.kind == WatchKind.DEADLINE:
        found = _check_deadline(watcher, now)

    watcher.last_checked_at = now.isoformat(timespec="seconds")
    if found:
        watcher.last_fired_at = watcher.last_checked_at
        watcher.fire_count += len(found)
        # Attaching evidence is the whole mechanism: it makes the thread due
        # for the worker, which is what turns a match into an explanation.
        for ref in found:
            db.add_evidence(Evidence(
                thread_id=watcher.thread_id,
                kind=ref["kind"],
                ref_id=ref["ref_id"],
                note=f"watcher: {watcher.what}"[:200],
            ))
    db.save_watcher(watcher)
    return found


def _check_mail(watcher: Watcher, since: str) -> List[Dict[str, str]]:
    from ..assistant import tools

    hits = tools.search_mail(
        query=watcher.spec.get("query"),
        sender=watcher.spec.get("sender"),
        label=watcher.spec.get("label"),
        since=since,
        direction="from_them",
        limit=5,
    )
    return [{"kind": "message", "ref_id": h["message_id"]} for h in hits]


def _check_messages(watcher: Watcher, since: str) -> List[Dict[str, str]]:
    from ..assistant import tools

    hits = tools.search_messages(
        query=watcher.spec.get("query"),
        person_id=watcher.spec.get("person_id"),
        since=since,
        direction="from_them",
        limit=5,
    )
    return [{"kind": "message", "ref_id": h["message_id"]} for h in hits]


def _check_calendar(watcher: Watcher, since: str) -> List[Dict[str, str]]:
    """Calendar events that appeared or moved since the last look. `updated_at`
    is what makes a *change* visible — a flight moved is the same event with a
    new time, and matching on the id alone would never notice."""
    needle = (watcher.spec.get("query") or "").strip().lower()
    out = []
    for event in db.list_calendar_events():
        if (event.updated_at or "") <= since:
            continue
        haystack = f"{event.summary} {event.description} {event.location}".lower()
        if needle and needle not in haystack:
            continue
        out.append({"kind": "calendar_event", "ref_id": event.id})
    return out[:5]


def _check_deadline(watcher: Watcher, now: datetime) -> List[Dict[str, str]]:
    """The watcher for when *nothing arrives but time passes*. Without it a
    thread with a date and a quiet inbox is never revisited — the whole system
    would only ever react to other people."""
    from ..extraction.dates import days_until

    thread = db.get_thread(watcher.thread_id)
    if not thread or not thread.deadline:
        return []
    days = days_until(thread.deadline, now)
    window = float(watcher.spec.get("days_before", 3))
    if days is None or days > window or days < -1:
        return []
    # Nothing new to attach — the *thread itself* is the news. Touching it is
    # what makes the worker pick it up.
    thread.last_worked_at = None
    db.save_thread(thread)
    return []


def sweep(reference: Optional[datetime] = None) -> Dict[str, int]:
    """Run every due watcher. Called from the poller; costs no model calls."""
    now = reference or datetime.now(timezone.utc)
    checked = fired = expired = 0

    for watcher in due(now):
        thread = db.get_thread(watcher.thread_id)
        # A monitor that outlives its reason is a scheduled way to waste money.
        if (
            thread is None
            or thread.state in ThreadState.CLOSED
            or (watcher.until and (parse_iso(watcher.until) or now) < now)
        ):
            watcher.state = "expired"
            db.save_watcher(watcher)
            expired += 1
            continue

        found = check(watcher, now)
        checked += 1
        if found:
            fired += 1
            log.info("watcher fired on %r: %s (%d)", thread.title, watcher.what, len(found))

    return {"checked": checked, "fired": fired, "expired": expired}
