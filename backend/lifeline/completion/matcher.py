"""Evidence matching for the completion engine (§7).

Given an open item and a piece of new external data (an email, a calendar
event), decide how strongly the data says "this is done". Confidence is built
from named, additive reasons rather than one opaque similarity number, because
the resolution rule — auto-close vs. ask vs. ignore — has to be defensible to
the user when it closes something on their behalf.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

from ..extraction.dates import days_until
from ..models import CalendarEvent, Item, Message, parse_iso

# Confidence bands (§7 steps 3-5).
AUTO_CLOSE = 0.80
NEEDS_CONFIRMATION = 0.45

_WORD = re.compile(r"[a-z0-9']{3,}")
_MONEY = re.compile(r"[$£€]\s?([\d,]+(?:\.\d{2})?)")
_ORDER_REF = re.compile(r"\b(?:order|confirmation|conf|reference|booking)\s*#?\s*([A-Z0-9-]{5,})\b", re.I)

_STOP = {
    "the", "and", "for", "you", "your", "our", "with", "that", "this", "have", "has",
    "was", "are", "will", "would", "can", "get", "got", "about", "from", "there",
    "here", "them", "they", "she", "her", "his", "him", "its", "not", "but", "all",
    "any", "one", "two", "out", "who", "how", "why", "when", "what", "need", "want",
    "please", "thanks", "thank", "sent", "send", "know", "let", "just", "still",
    "today", "tomorrow", "week", "time", "thing", "things", "some", "more", "been",
}

# Language that means "this happened", as opposed to merely mentioning it.
_CONFIRMATION_LANGUAGE = re.compile(
    r"\b(confirmed|confirmation|your order|order #|receipt|thank you for your (?:order|purchase|booking)|"
    r"has shipped|shipped|out for delivery|delivered|booked|reservation confirmed|"
    r"you'?re (?:registered|booked|confirmed)|registration complete|payment received|"
    r"we'?ve received your|ticket|itinerary|e-ticket|appointment (?:confirmed|scheduled))\b",
    re.I,
)
_CANCELLATION_LANGUAGE = re.compile(r"\b(cancell?ed|refunded|declined|failed|could not be|unable to)\b", re.I)

# Tokens too common to establish identity on their own — "birthday" matching
# "birthday" is not evidence that a specific gift was bought.
_GENERIC = {
    "birthday", "dinner", "lunch", "party", "event", "meeting", "call", "appointment",
    "trip", "visit", "weekend", "morning", "evening", "night", "family", "friend",
    "home", "house", "work", "kids", "book", "gift", "present", "order", "date",
}

# Receipts almost never reuse the sender's phrasing. These bridges connect
# "book the flights" to an airline itinerary that shares no vocabulary with it.
CATEGORY_SYNONYMS: List[Tuple[str, str, str]] = [
    ("travel", r"\b(flight|flights|fly|flying|airfare|plane|airline|itinerary|boarding)\b",
     r"\b(itinerary|boarding|departing|arrival|airline|airways|flight|e-?ticket|confirmation code|"
     r"alaska|united|delta|southwest|jetblue|american airlines)\b"),
    ("lodging", r"\b(hotel|airbnb|motel|stay|lodging|room)\b",
     r"\b(reservation|check-?in|check-?out|nights?|booking confirmed|hotel|airbnb)\b"),
    ("medical", r"\b(doctor|pediatrician|dentist|clinic|appointment|checkup|prescription)\b",
     r"\b(appointment|clinic|dr\.?|doctor|health|medical|pediatric|scheduled with)\b"),
    ("registration", r"\b(register|registration|sign up|entry|enter|enroll)\b",
     r"\b(registered|registration (?:complete|confirmed)|you'?re in|entry confirmed|bib)\b"),
    ("payment", r"\b(pay|paid|send|venmo|zelle|transfer|reimburse|owe|fee)\b",
     r"\b(payment (?:received|sent|confirmed)|you paid|receipt|transaction|invoice paid)\b"),
]


@dataclass
class Match:
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    evidence_ref: str = ""
    evidence_text: str = ""
    source: str = "mail"

    @property
    def resolution(self) -> Optional[str]:
        if self.confidence >= AUTO_CLOSE:
            return "auto_closed"
        if self.confidence >= NEEDS_CONFIRMATION:
            return "needs_confirmation"
        return None


def tokens(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower())} - _STOP


def phrase_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def entity_overlap(item: Item, haystack: str) -> Tuple[float, Optional[str]]:
    """How much of the item's entity appears in the candidate evidence."""
    entity = (item.entities.item or "").strip()
    if not entity:
        return 0.0, None
    hay = haystack.lower()
    if entity.lower() in hay:
        return 1.0, f"exact phrase “{entity}” appears in the evidence"

    entity_tokens = tokens(entity)
    if not entity_tokens:
        return 0.0, None
    hit = entity_tokens & tokens(haystack)
    # A hit made up only of generic words is a coincidence, not a match.
    if hit and not (hit - _GENERIC):
        hit = set()
    ratio = len(hit) / len(entity_tokens)

    if ratio >= 0.75:
        return ratio, f"most of “{entity}” matches ({', '.join(sorted(hit))})"
    # Near-miss spellings: "croissant bag" vs "Croissant Bag, Nubuck".
    best = max((phrase_similarity(entity, line) for line in hay.splitlines() if line.strip()), default=0.0)
    if best >= 0.6:
        return best * 0.8, f"“{entity}” closely resembles text in the evidence"
    if ratio > 0:
        return ratio * 0.6, f"partial match on “{entity}” ({', '.join(sorted(hit))})"
    return 0.0, None


def category_bridge(item: Item, haystack: str) -> Optional[str]:
    """Shared domain between the commitment and the evidence, where the two
    share no literal vocabulary — "book the flights" vs. an airline itinerary."""
    source = f"{item.raw_text} {item.entities.item or ''} {item.suggested_action}"
    for name, item_pattern, evidence_pattern in CATEGORY_SYNONYMS:
        if re.search(item_pattern, source, re.I) and re.search(evidence_pattern, haystack, re.I):
            return f"both are about {name}"
    return None


def amount_agreement(item: Item, haystack: str) -> Optional[str]:
    """"$88" and "$88.00" are the same amount."""
    def values(text: str) -> set:
        out = set()
        for raw in _MONEY.findall(text):
            try:
                out.add(round(float(raw.replace(",", "")), 2))
            except ValueError:
                continue
        return out

    wanted, found = values(item.raw_text), values(haystack)
    if wanted and found and (wanted & found):
        return "the amount matches"
    return None


def date_agreement(item: Item, evidence_date: Optional[str], tolerance_days: float = 2.0) -> Optional[str]:
    target, evidence = parse_iso(item.entities.date), parse_iso(evidence_date)
    if not target or not evidence:
        return None
    delta = abs((evidence - target).total_seconds()) / 86400.0
    if delta <= tolerance_days:
        return "the date lines up with the item's date"
    return None


# ------------------------------------------------------------------- mail
def match_email(item: Item, message: Message, evidence_floor: Optional[str] = None) -> Match:
    """Score an email as evidence that *item* is done.

    ``evidence_floor`` is the earliest timestamp that can count; the caller
    widens it for follow-ups, whose evidence may pre-date the nudge.
    """
    match = Match(source="mail", evidence_ref=message.id)
    metadata = message.metadata or {}
    subject = metadata.get("subject", "")
    haystack = f"{subject}\n{message.text}"
    match.evidence_text = (subject or message.text)[:200]

    if message.timestamp <= (evidence_floor or item.timestamp):
        return match
    if _CANCELLATION_LANGUAGE.search(haystack):
        return match

    confidence = 0.0
    reasons: List[str] = []

    overlap, note = entity_overlap(item, haystack)
    if note:
        reasons.append(note)
    confidence += overlap * 0.55

    confirms = bool(_CONFIRMATION_LANGUAGE.search(haystack))
    if confirms:
        confidence += 0.3
        reasons.append("the email is a confirmation or receipt")
    if metadata.get("purchase") and item.type == "purchase":
        confidence += 0.15
        reasons.append("Gmail categorised it as a purchase")
    if _ORDER_REF.search(haystack):
        confidence += 0.05
        reasons.append("it carries an order/confirmation reference")

    amount = amount_agreement(item, haystack)
    if amount:
        confidence += 0.15
        reasons.append(amount)

    date_note = date_agreement(item, message.timestamp)
    if date_note:
        confidence += 0.05
        reasons.append(date_note)

    bridge = category_bridge(item, haystack)
    if bridge:
        confidence += 0.3
        reasons.append(bridge)

    # A receipt with no tie at all to the item is somebody else's receipt.
    if overlap <= 0.0 and not bridge:
        confidence = min(confidence, 0.25)
    elif overlap <= 0.0 and confirms:
        # A domain match plus a confirmation is suggestive, never conclusive.
        confidence = min(confidence, AUTO_CLOSE - 0.05)

    # Reading items are closed by opening a link, not by email.
    if item.type == "reading":
        confidence *= 0.4

    match.confidence = round(min(confidence, 0.99), 3)
    match.reasons = reasons
    return match


# --------------------------------------------------------------- calendar
def match_calendar(item: Item, event: CalendarEvent, reference: Optional[datetime] = None) -> Match:
    """Score a calendar event as evidence. §7: "calendar event confirmed past
    with no cancellation" is the high-confidence case."""
    now = reference or datetime.now(timezone.utc)
    match = Match(source="calendar", evidence_ref=event.id, evidence_text=event.summary[:200])

    if event.status == "cancelled":
        return match

    haystack = f"{event.summary}\n{event.description}\n{event.location}"
    overlap, note = entity_overlap(item, haystack)
    if not overlap:
        # Fall back to the raw text — items often lack a clean entity.
        item_tokens = tokens(item.raw_text)
        hit = item_tokens & tokens(haystack)
        if len(hit) >= 2:
            overlap = min(0.8, len(hit) / max(len(item_tokens), 1) + 0.3)
            note = f"the event text overlaps the item ({', '.join(sorted(hit)[:4])})"
    if not overlap:
        return match

    confidence = overlap * 0.5
    reasons = [note] if note else []

    days = days_until(event.start_at, now)
    if days is None:
        return match

    if days < 0:
        end_passed = days_until(event.end_at or event.start_at, now)
        if end_passed is not None and end_passed < 0:
            confidence += 0.35
            reasons.append(f"“{event.summary}” took place on {(event.start_at or '')[:10]} and wasn't cancelled")
    else:
        # A future booking is evidence the *arranging* is done, not the event.
        if item.type in ("promise", "purchase", "question", "followup"):
            confidence += 0.3
            reasons.append(f"“{event.summary}” is on the calendar for {(event.start_at or '')[:10]}")
        else:
            # An `event` item whose event hasn't happened yet is not done, and
            # asking "did you finish Grandma's 80th?" beforehand is nonsense.
            match.confidence = min(round(confidence + 0.05, 3), NEEDS_CONFIRMATION - 0.05)
            match.reasons = reasons + [f"“{event.summary}” is scheduled but hasn't happened yet"]
            return match

    if event.self_response == "accepted":
        confidence += 0.15
        reasons.append("you've RSVP'd yes")
    elif event.self_response == "declined":
        confidence += 0.1
        reasons.append("you've RSVP'd no — either way it's answered")
    elif event.self_response == "needsAction" and item.type in ("event", "question"):
        # An outstanding RSVP is the opposite of evidence.
        confidence -= 0.3
        reasons.append("RSVP is still outstanding")

    match.confidence = round(max(0.0, min(confidence, 0.99)), 3)
    match.reasons = reasons
    return match
