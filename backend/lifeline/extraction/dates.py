"""Relative -> absolute date normalisation (§5, `event` requires it).

Casual conversation almost never contains an absolute date. "in 3 weeks",
"a week from Saturday", "by Friday", "the 4th" all resolve against the
message's own timestamp, not against wall-clock now — a three-day-old export
must not drift.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ..models import parse_iso

# The timezone a day is judged in. None means the system's own — the backend
# runs on the user's machine, so the system day IS the user's day. Tests pin
# it (e.g. ZoneInfo("America/New_York")) so they don't depend on the runner.
LOCAL_TZ = None

# Verbs that put a date in the past. "The party is January 4" rolls into next
# year when January is gone; "your payment on Aug 3 failed" must not — the
# failure happened, and a rolled year turns an overdue bill into a deadline
# three weeks... next year (the live Capital One thread carried 2027-08-03).
_PAST_TENSE = re.compile(
    r"\b(failed|was|were|happened|received|sent|paid|charged|missed|expired|"
    r"declined|posted|arrived|billed|processed|returned|bounced|last)\b", re.I)


def _looks_ahead(text: str) -> bool:
    return has_deadline_language(text) or not _PAST_TENSE.search(text or "")

_URL = re.compile(r"https?://\S+")

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
_DAY_ALT = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_NUM_ALT = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))

_RE_IN_N = re.compile(rf"\bin\s+(?P<n>\d+|{_NUM_ALT})\s+(?P<unit>day|week|month)s?\b", re.I)
_RE_N_FROM_DAY = re.compile(rf"\b(?P<n>\d+|{_NUM_ALT})\s+weeks?\s+from\s+(?P<day>{_DAY_ALT})\b", re.I)
_RE_MONTH_DAY = re.compile(rf"\b(?P<month>{_MONTH_ALT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)
_RE_DAY_MONTH = re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>{_MONTH_ALT})\b", re.I)
_RE_NUMERIC = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\b")
_RE_ORDINAL = re.compile(r"\bthe\s+(?P<day>\d{1,2})(?:st|nd|rd|th)\b", re.I)
_RE_WEEKDAY = re.compile(rf"\b(?P<mod>next|this|coming)?\s*(?P<day>{_DAY_ALT})\b", re.I)
_RE_TOMORROW = re.compile(r"\b(tomorrow|tmrw|tmw)\b", re.I)
_RE_TONIGHT = re.compile(r"\b(tonight|today)\b", re.I)
_RE_WEEKEND = re.compile(r"\b(this|next)?\s*weekend\b", re.I)
_RE_EOW = re.compile(r"\bend of (the )?week\b", re.I)
_RE_EOM = re.compile(r"\bend of (the )?month\b", re.I)
_RE_MONTH_ONLY = re.compile(rf"\bin\s+(?P<month>{_MONTH_ALT})\b", re.I)

# "by Friday", "before the 5th" — a deadline is still a date, and the ranking
# engine cares more about deadlines than about mentions.
_DEADLINE_PREFIX = re.compile(r"\b(by|before|due|no later than|deadline|closes?|ends?)\b", re.I)


def _n(token: str) -> int:
    token = token.strip().lower()
    return int(token) if token.isdigit() else NUMBER_WORDS.get(token, 1)


def _at_noon(d: date) -> datetime:
    """Date-only references get noon UTC: avoids off-by-one across timezones."""
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)


def _next_weekday(anchor: date, weekday: int, force_next_week: bool = False) -> date:
    """The coming *weekday*, strictly after *anchor*.

    ``force_next_week`` handles "next Friday", which means the Friday of the
    following calendar week rather than the one a couple of days away.
    """
    if force_next_week:
        next_monday = anchor + timedelta(days=7 - anchor.weekday())
        return next_monday + timedelta(days=weekday)
    delta = (weekday - anchor.weekday()) % 7 or 7
    return anchor + timedelta(days=delta)


def _clamp_day(year: int, month: int, day: int) -> date:
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
            if day < 1:
                return date(year, month, 1)


def _forward(candidate: date, anchor: date, grace_days: int = 2,
             looking_ahead: bool = True) -> date:
    """Conversation dates *usually* point forward; roll the year if we landed
    in the past — but only when the text is actually looking ahead. "Your
    payment on Aug 3 failed", read on Aug 11, is about THIS August: rolling it
    produced a thread carrying deadline 2027-08-03, ranked three weeks away
    forever (audit finding #6's second half)."""
    if candidate >= anchor - timedelta(days=grace_days):
        return candidate
    if not looking_ahead:
        return candidate                     # a past event stays in the past
    try:
        return candidate.replace(year=candidate.year + 1)
    except ValueError:
        return candidate + timedelta(days=365)


def normalise(text: str, anchor: Optional[datetime] = None, weak: bool = True) -> Optional[str]:
    """Return the first date reference in *text* as ISO-8601, or None.

    ``weak=False`` disables the loose markers — a bare weekday, "today",
    "the weekend" — which are usually narration ("saw it in the window
    today") rather than a due date. Callers scanning a whole message for a
    fallback date pass ``weak=False`` unless the text carries deadline
    language.
    """
    if not text:
        return None
    text = _URL.sub(" ", text)      # dates inside URLs are not dates
    anchor_dt = anchor or datetime.now(timezone.utc)
    if anchor_dt.tzinfo is None:
        anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
    # The sender's day, not Greenwich's (audit finding #6). Messages carry UTC
    # timestamps, and after 8pm EDT the UTC calendar has already turned: with
    # a UTC base, "tomorrow" said at 9pm was two days out, "tonight" was the
    # wrong day, and "by Friday" said Thursday evening became *next* Friday.
    base = anchor_dt.astimezone(LOCAL_TZ).date()

    m = _RE_N_FROM_DAY.search(text)
    if m:
        # "a week from Saturday" is the Saturday after the coming one.
        target = _next_weekday(base, WEEKDAYS[m.group("day").lower()])
        return _at_noon(target + timedelta(weeks=_n(m.group("n")))).isoformat()

    m = _RE_IN_N.search(text)
    if m:
        count, unit = _n(m.group("n")), m.group("unit").lower()
        days = {"day": 1, "week": 7, "month": 30}[unit] * count
        return _at_noon(base + timedelta(days=days)).isoformat()

    for regex, order in ((_RE_MONTH_DAY, "md"), (_RE_DAY_MONTH, "dm")):
        m = regex.search(text)
        if m:
            month = MONTHS[m.group("month").lower()]
            day = int(m.group("day"))
            return _at_noon(_forward(_clamp_day(base.year, month, day), base,
                                     looking_ahead=_looks_ahead(text))).isoformat()

    m = _RE_NUMERIC.search(text)
    if m:
        month, day = int(m.group("m")), int(m.group("d"))
        if 1 <= month <= 12 and 1 <= day <= 31:
            year = int(m.group("y") or base.year)
            if year < 100:
                year += 2000
            candidate = _clamp_day(year, month, day)
            return _at_noon(candidate if m.group("y") else _forward(
                candidate, base, looking_ahead=_looks_ahead(text))).isoformat()

    if _RE_TOMORROW.search(text):
        return _at_noon(base + timedelta(days=1)).isoformat()

    if not weak:
        return None

    m = _RE_WEEKEND.search(text)
    if m:
        saturday = _next_weekday(base, 5)
        if (m.group(1) or "").lower() == "next":
            saturday += timedelta(days=7)
        return _at_noon(saturday).isoformat()

    if _RE_EOW.search(text):
        return _at_noon(_next_weekday(base, 4)).isoformat()

    if _RE_EOM.search(text):
        first_next = _clamp_day(base.year + (base.month == 12), (base.month % 12) + 1, 1)
        return _at_noon(first_next - timedelta(days=1)).isoformat()

    m = _RE_WEEKDAY.search(text)
    if m:
        weekday = WEEKDAYS[m.group("day").lower()]
        force_next = (m.group("mod") or "").lower() == "next"
        return _at_noon(_next_weekday(base, weekday, force_next)).isoformat()

    m = _RE_ORDINAL.search(text)
    if m:
        day = int(m.group("day"))
        candidate = _clamp_day(base.year, base.month, day)
        if candidate < base:
            month = (base.month % 12) + 1
            year = base.year + (1 if month == 1 else 0)
            candidate = _clamp_day(year, month, day)
        return _at_noon(candidate).isoformat()

    if _RE_TONIGHT.search(text):
        return _at_noon(base).isoformat()

    m = _RE_MONTH_ONLY.search(text)
    if m:
        month = MONTHS[m.group("month").lower()]
        return _at_noon(_forward(_clamp_day(base.year, month, 1), base, grace_days=15,
                                 looking_ahead=_looks_ahead(text))).isoformat()

    return None


def has_deadline_language(text: str) -> bool:
    """Distinguishes "dinner is Saturday" from "RSVP by Saturday"."""
    return bool(_DEADLINE_PREFIX.search(text or ""))


def days_until(iso_value: Optional[str], reference: Optional[datetime] = None) -> Optional[float]:
    target = parse_iso(iso_value)
    if not target:
        return None
    ref = reference or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return (target - ref).total_seconds() / 86400.0
