"""The individual signals feeding the importance score (§6.2).

Each function returns ``(value, detail)`` where value is roughly in [-1, 1] and
detail is a human-readable string. The detail strings are what the app shows
under "why this is ranked here" (§8.3) — the engine is required to be
inspectable, not a black box, so every signal explains itself.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from .. import db
from . import relationships
from ..extraction.dates import days_until
from ..models import Item, parse_iso

Signal = Tuple[float, str]

# Defaults used until the learning loop (§6.3 / milestone 7) has evidence.
RELATIONSHIP_PRIOR = {
    "spouse": 1.00,
    "family": 0.70,
    "friend": 0.60,
    "colleague": 0.45,
    None: 0.35,
}

TYPE_PRIOR = {
    "promise": 0.80,
    "followup": 0.75,
    "event": 0.70,
    "question": 0.60,
    "purchase": 0.50,
    "reading": 0.30,
}

# Work that has to happen *before* a dated thing, not on the day of it.
_DEPENDENT_ACTION = re.compile(
    r"\b(book|booking|flight|flights|fly|train|hotel|ticket|tickets|reserve|reservation|"
    r"rsvp|register|registration|order|buy|ship|shipping|deliver|delivery|gift|present|"
    r"cake|permit|visa|passport|sitter|babysitter|dog sitter|caterer)\b",
    re.I,
)
_LEAD_TIME_DAYS = 14.0

# Words too common to carry a match on their own.
_GENERIC_TOKENS = {
    "follow", "followup", "phone", "call", "thing", "week", "weeks", "time", "today",
    "tomorrow", "know", "need", "want", "some", "just", "really", "going", "back",
    "with", "that", "this", "have", "been", "about", "there", "their", "would",
    "please", "thanks", "sent", "send", "meet", "make", "take", "email", "morning",
    "night", "reminder", "confirmed", "booked",
}

_FLEXIBLE = re.compile(
    r"\b(no rush|whenever|no pressure|if you (?:get|have) a (?:sec|chance|minute)|sometime|"
    r"at some point|eventually|when you can|no hurry|not urgent)\b",
    re.I,
)
_SOCIAL_STAKES = re.compile(
    r"\b(call|phone|apologi[sz]e|sorry|talk to|confront|difficult|decide|decision|sign|"
    r"doctor|pediatrician|lawyer|money|owe|pay|argument|upset|disappointed|serious)\b",
    re.I,
)


def _now(reference: Optional[datetime] = None) -> datetime:
    return reference or datetime.now(timezone.utc)


# ------------------------------------------------------------------ 6.2.1
def deadline_pressure(item: Item, reference: Optional[datetime] = None) -> Signal:
    """A hard date approaching is the strongest single signal."""
    days = days_until(item.entities.date, _now(reference))
    if days is None:
        return 0.0, "no date attached"
    if days < -1:
        return -0.4, f"date passed {abs(days):.0f} days ago"
    if days <= 1:
        return 1.0, "due within 24 hours"
    if days <= 3:
        return 0.85, f"due in {days:.0f} days"
    if days <= 7:
        return 0.6, f"due in {days:.0f} days"
    if days <= 21:
        return 0.3, f"due in {days:.0f} days"
    return 0.1, f"due in {days:.0f} days"


def dependency_pressure(item: Item, reference: Optional[datetime] = None) -> Signal:
    """§6.1 — "dependent actions (e.g. travel booking tied to an event date)".

    A booking for an event three weeks out is not a three-week-out task; it is
    due now. Where the item names dependent work and lines up with a calendar
    event, the deadline is pulled forward by the lead time.
    """
    # Only the user's own words — never suggested_action, whose template
    # wording ("follow through on…") would collide with event descriptions.
    text = f"{item.raw_text} {item.entities.item or ''}"
    if not _DEPENDENT_ACTION.search(text):
        return 0.0, "no dependent action"

    tokens = _tokens(text) - _GENERIC_TOKENS
    best: Optional[Tuple[float, str]] = None
    for event in db.list_calendar_events():
        if event.status == "cancelled":
            continue
        days = days_until(event.start_at, _now(reference))
        if days is None or days < 0:
            continue
        # A hit on the event *title* is meaningful on its own; a hit buried in
        # the description needs corroboration before it moves a rank.
        title_overlap = tokens & (_tokens(event.summary) - _GENERIC_TOKENS)
        body_overlap = tokens & (_tokens(f"{event.description} {event.location}") - _GENERIC_TOKENS)
        if not title_overlap and len(body_overlap) < 2:
            continue
        effective = days - _LEAD_TIME_DAYS
        pressure = 1.0 if effective <= 0 else max(0.0, 1.0 - (effective / 30.0))
        if pressure > 0 and (best is None or pressure > best[0]):
            best = (pressure, f"prep work for “{event.summary}” in {days:.0f} days")
    if not best:
        return 0.0, "no dependent action"
    return round(best[0], 3), best[1]


# ------------------------------------------------------------------ 6.2.2
def sender_weight(item: Item) -> Signal:
    """Who this is from, as importance. Behavioural learning wins once we have
    it; before that, tie strength inferred from your actual conversations —
    not the almost-always-unset relationship label — is the cold start."""
    if not item.person_id:
        return RELATIONSHIP_PRIOR[None], "unknown sender"
    learned = db.get_weight_row(f"person:{item.person_id}")
    if learned and learned["observations"] >= 3:
        return learned["value"], f"{item.person}: learned weight {learned['value']:.2f}"
    person = db.get_person(item.person_id)
    prior = RELATIONSHIP_PRIOR.get(person.relationship if person else None, RELATIONSHIP_PRIOR[None])
    tie = relationships.strength(item.person_id)
    tie_weight = 0.35 + 0.6 * tie                 # 0..1 tie strength -> 0.35..0.95
    if tie_weight >= prior:
        return round(tie_weight, 3), f"{item.person}: {relationships.describe(item.person_id)}"
    label = (person.relationship if person else None) or "no relationship set"
    return prior, f"{item.person} ({label})"


def type_weight(item: Item) -> Signal:
    learned = db.get_weight_row(f"type:{item.type}")
    prior = TYPE_PRIOR.get(item.type, 0.5)
    if learned and learned["observations"] >= 5:
        return learned["value"], f"{item.type} items: learned weight {learned['value']:.2f}"
    return prior, f"{item.type} items"


def pair_weight(item: Item) -> Signal:
    """Sender x type — "Tess's purchase requests" behaves unlike "Dev's links"."""
    if not item.person_id:
        return 0.0, "no sender/type history"
    learned = db.get_weight_row(f"pair:{item.person_id}/{item.type}")
    if not learned or learned["observations"] < 3:
        return 0.0, "no sender/type history"
    delta = learned["value"] - 0.5
    direction = "prioritised" if delta >= 0 else "deprioritised"
    return delta * 2, f"{item.person}'s {item.type} items are historically {direction}"


# ------------------------------------------------------------------ 6.2.3
def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9']{4,}", (text or "").lower())}


def response_latency(item: Item) -> Signal:
    """How fast the user historically replies to this sender."""
    if not item.person_id:
        return 0.0, "no reply history"
    latencies = person_reply_latencies(item.person_id)
    if not latencies:
        return 0.0, "no reply history"
    median_hours = statistics.median(latencies)
    if median_hours <= 1:
        return 1.0, f"you usually reply to {item.person} within the hour"
    if median_hours <= 6:
        return 0.6, f"you usually reply to {item.person} within {median_hours:.0f}h"
    if median_hours <= 24:
        return 0.25, f"you usually reply to {item.person} within a day"
    return -0.2, f"you typically take {median_hours / 24:.0f} days to reply to {item.person}"


def person_reply_latencies(person_id: str, max_gap_hours: float = 72.0) -> List[float]:
    """Hours between a message from *person_id* and the user's next reply."""
    latencies: List[float] = []
    for thread in db.list_conversations():
        messages = db.thread_messages(thread.id)
        pending_at: Optional[datetime] = None
        for message in messages:
            stamp = parse_iso(message.timestamp)
            if stamp is None:
                continue
            if message.is_from_user:
                if pending_at is not None:
                    gap = (stamp - pending_at).total_seconds() / 3600.0
                    if 0 <= gap <= max_gap_hours:
                        latencies.append(gap)
                    pending_at = None
            elif message.person_id == person_id and pending_at is None:
                pending_at = stamp
    return latencies


def action_immediacy(item: Item) -> Signal:
    """§6.2 — acting immediately after an item surfaces is a strong positive
    signal for that sender/type combination going forward."""
    if not item.person_id:
        return 0.0, "no action history for this pairing"
    ratio, sample = immediacy_ratio(item.person_id, item.type)
    if sample < 2:
        return 0.0, "no action history for this pairing"
    if ratio >= 0.6:
        return 0.8, f"you act fast on {item.person}'s {item.type} items ({int(ratio * 100)}% same-session)"
    if ratio <= 0.2:
        return -0.4, f"you rarely act on {item.person}'s {item.type} items when shown"
    return 0.1, f"mixed history on {item.person}'s {item.type} items"


def immediacy_ratio(person_id: str, item_type: str, window_hours: float = 1.0) -> Tuple[float, int]:
    """Share of surfaced items acted on within *window_hours*."""
    rows = db.behavior_events(person_id=person_id, item_type=item_type)
    surfaced: Dict[str, datetime] = {}
    acted: Dict[str, datetime] = {}
    for row in rows:
        stamp = parse_iso(row["occurred_at"])
        if stamp is None or not row["item_id"]:
            continue
        if row["kind"] == "surfaced":
            surfaced.setdefault(row["item_id"], stamp)
        elif row["kind"] in ("acted", "completed_manual", "replied"):
            acted.setdefault(row["item_id"], stamp)
    if not surfaced:
        return 0.0, 0
    quick = sum(
        1
        for item_id, shown in surfaced.items()
        if item_id in acted and 0 <= (acted[item_id] - shown).total_seconds() / 3600.0 <= window_hours
    )
    return quick / len(surfaced), len(surfaced)


# ------------------------------------------------------------------ 6.2.4
def explicit_signals(item: Item) -> Signal:
    """Starred/flagged in Gmail, pinned conversation, etc."""
    message = db.get_message(item.message_id) if item.message_id else None
    metadata = message.metadata if message else {}
    marks = []
    value = 0.0
    if metadata.get("starred"):
        value += 0.7
        marks.append("starred in Gmail")
    if metadata.get("important"):
        value += 0.3
        marks.append("marked important")
    if metadata.get("pinned"):
        value += 0.5
        marks.append("pinned conversation")
    if metadata.get("promotional"):
        value -= 0.6
        marks.append("promotional mail")
    if not marks:
        return 0.0, "no explicit flags"
    return max(-1.0, min(1.0, value)), ", ".join(marks)


def language_tone(item: Item) -> Signal:
    """The sender's own framing. "no rush" is data."""
    if _FLEXIBLE.search(item.raw_text):
        return -0.6, "sender explicitly said it's not urgent"
    return 0.0, "neutral framing"


def emotional_weight(item: Item) -> Signal:
    """§6.3 — items with social stakes are the ones that get avoided."""
    if _SOCIAL_STAKES.search(f"{item.raw_text} {item.suggested_action}"):
        return 0.35, "requires a call, decision, or has social stakes"
    return 0.0, "low social stakes"


def staleness(item: Item, reference: Optional[datetime] = None) -> Signal:
    """Recency decay on the *conversation*, not the ingest time. A briefing is
    about what's live now; a thing said months ago that you never came back to
    should sink, not sit at the top forever. (The old version measured
    ``created_at`` — the moment we imported the message — so a 90-day backfill
    looked uniformly "new" and the entire history landed on Today at once.)"""
    when = parse_iso(item.timestamp) or _now(reference)
    age_days = (_now(reference) - when).total_seconds() / 86400.0
    if age_days < 2:
        return 0.3, "from today"
    if age_days < 7:
        return 0.15, f"from {age_days:.0f} days ago"
    if age_days < 21:
        return -0.1, f"from {age_days / 7:.0f} weeks ago"
    if age_days < 60:
        return -0.4, "from over a month ago"
    return -0.7, f"from {age_days / 30:.0f} months ago"


def unread(item: Item, reference: Optional[datetime] = None) -> Signal:
    """An email you haven't even opened is unhandled by definition — and more so
    if it's already due. Read-state is captured at ingest; once you open (or
    reply), the currency signal takes over."""
    if item.source != "gmail" or not item.message_id:
        return 0.0, "not email"
    message = db.get_message(item.message_id)
    labels = (message.metadata.get("labels") if message else None) or []
    if "UNREAD" not in labels:
        return 0.0, "you've opened this"
    days = days_until(item.entities.date, _now(reference))
    if days is not None and days <= 2:
        return 0.7, "unopened, and due now"
    return 0.35, "you haven't opened this yet"


def currency(item: Item, reference: Optional[datetime] = None) -> Signal:
    """Has the conversation already moved past this? If the user themselves sent
    a reply in the thread after the item's message, the loop is very likely
    already closed — so it should fall off the briefing rather than nag. This is
    the single biggest lever against resurfacing already-handled items."""
    if not item.conversation_id:
        return 0.0, "no thread"
    reply = db.user_reply_after(item.conversation_id, item.timestamp)
    if reply is None:
        return 0.0, "no reply from you since"
    return -1.0, "you already replied after this in the thread"


# ------------------------------------------------------------------ 6.4
def time_of_day(item: Item, reference: Optional[datetime] = None) -> Signal:
    """Mid-morning is the user's analytical peak and stated check-in window,
    so decision-requiring work is weighted up then and Passive work is held."""
    hour = _now(reference).astimezone().hour
    morning = 6 <= hour < 11
    decision_type = item.type in ("promise", "followup", "question", "event")
    if morning and decision_type:
        return 0.4, "morning: decision-requiring work weighted up"
    if morning and not decision_type:
        return -0.2, "morning: low-stakes work held back"
    if hour >= 21 or hour < 6:
        return -0.25 if decision_type else 0.1, "late: analytical work deferred"
    return 0.0, "midday: neutral"
