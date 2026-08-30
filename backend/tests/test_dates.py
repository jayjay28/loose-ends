"""Relative -> absolute normalisation (§5)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lifeline.extraction import dates

# A Tuesday.
ANCHOR = datetime(2026, 7, 21, 15, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Grandma's 80th is in 3 weeks", "2026-08-11"),
        ("in two weeks", "2026-08-04"),
        ("in 5 days", "2026-07-26"),
        ("a week from Saturday", "2026-08-01"),
        ("two weeks from Monday", "2026-08-10"),
        ("dinner on August 9", "2026-08-09"),
        ("the 9th of August", "2026-08-09"),
        ("closes Aug 1", "2026-08-01"),
        ("tomorrow", "2026-07-22"),
        ("by Friday", "2026-07-24"),
        ("next Monday", "2026-07-27"),
        ("this weekend", "2026-07-25"),
        ("end of the week", "2026-07-24"),
        ("end of the month", "2026-07-31"),
        ("on the 4th", "2026-08-04"),
        ("8/14", "2026-08-14"),
    ],
)
def test_normalises_relative_language(text, expected):
    assert (dates.normalise(text, ANCHOR) or "")[:10] == expected


def test_rolls_forward_into_next_year():
    # January, mentioned in July, means next January.
    assert (dates.normalise("the party is January 4", ANCHOR) or "")[:10] == "2027-01-04"


def test_ignores_dates_inside_urls():
    text = "read this https://example.com/magazine/2026/07/13/the-long-night"
    assert dates.normalise(text, ANCHOR) is None


def test_weak_markers_suppressed_when_requested():
    assert dates.normalise("saw it in the window today", ANCHOR, weak=False) is None
    assert dates.normalise("saw it in the window today", ANCHOR, weak=True) is not None


def test_strong_markers_survive_weak_false():
    assert (dates.normalise("closes August 5", ANCHOR, weak=False) or "")[:10] == "2026-08-05"


def test_no_date_returns_none():
    assert dates.normalise("thanks, that's great", ANCHOR) is None
    assert dates.normalise("", ANCHOR) is None


def test_deadline_language_detection():
    assert dates.has_deadline_language("need to know by Friday")
    assert dates.has_deadline_language("entry closes Aug 1")
    assert not dates.has_deadline_language("saw it in the window today")


def test_days_until_is_signed():
    assert dates.days_until("2026-07-23T12:00:00+00:00", ANCHOR) == pytest.approx(1.87, abs=0.05)
    assert dates.days_until("2026-07-20T12:00:00+00:00", ANCHOR) < 0
    assert dates.days_until(None, ANCHOR) is None


def test_february_29_clamps_in_common_year():
    anchor = datetime(2026, 2, 27, tzinfo=timezone.utc)
    # 2026 has no Feb 29; clamp rather than raise.
    assert (dates.normalise("February 29", anchor) or "")[:10] == "2026-02-28"


def test_evening_dates_resolve_on_the_senders_day_not_greenwich(monkeypatch):
    """Audit finding #6. Message timestamps are UTC, and after 8pm EDT the UTC
    calendar has already turned: "tomorrow" said at 9pm was two days out, and
    "by Friday" said Thursday evening became *next* Friday."""
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(dates, "LOCAL_TZ", ZoneInfo("America/New_York"))

    # 2026-08-21T01:00Z is Thursday Aug 20, 9pm in New York.
    evening = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)
    assert (dates.normalise("I'll send it tomorrow", evening) or "")[:10] == "2026-08-21"
    assert (dates.normalise("get it done tonight", evening) or "")[:10] == "2026-08-20"
    assert (dates.normalise("need it by Friday", evening) or "")[:10] == "2026-08-21", \
        "the Friday one day away, not eight"


def test_a_past_tense_date_stays_in_its_year(monkeypatch):
    """"Your payment on Aug 3 failed", read on Aug 11, is about THIS August.
    Rolling it forward gave the live Capital One thread deadline 2027-08-03,
    ranked as three weeks away forever."""
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(dates, "LOCAL_TZ", ZoneInfo("America/New_York"))
    anchor = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

    assert (dates.normalise("Your payment on Aug 3 failed", anchor) or "")[:10] == "2026-08-03"
    # ... while genuinely forward phrasing still rolls.
    assert (dates.normalise("rescheduled to Aug 3", anchor) or "")[:10] == "2027-08-03"
    assert (dates.normalise("pay by Aug 3", anchor) or "")[:10] == "2027-08-03", \
        "deadline language forces ahead even with no verb"
