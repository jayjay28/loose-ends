"""The interruption budget (§v2 step 7c).

The system **places a bet** on the user's attention; the swipe settles it. A
finding has to clear a learned bar to push, and everything else waits in the
app.

The spec locks every knob, so this is implementation rather than design:

    bar          0-1, starts at 0.6
    quiet        +0.05, capped at 0.95   ("right thread, wrong moment")
    dig in       -0.10, floored at 0.20  ("this matters more than you judged")
    decay        toward 0.6 by 0.01/day  so one bad week doesn't mute it forever
    scope        global — it models the user's attention, which is global
    hard cap     3 pushes/day, with the briefing and digest lanes for the rest
    quiet hours  inherited from config.morning_window

One line of that deserves its reason spelled out, because it is the whole point
of the step. **A bar alone is not a budget.** `notifications/scheduler.py`
already enforces one push per item ever, one briefing per day inside a window,
and one digest per 24h with a two-item minimum. "Clear a learned bar" has no
rate limit at all — ten findings clearing it in one poll cycle would send ten
pushes, which is looser than what v1.5 already ships. v2 must not regress the
thing it exists to improve.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .. import db
from ..config import get_config
from ..models import InterruptionLevel, ThreadOrigin, parse_iso

log = logging.getLogger(__name__)

BAR_KEY = "interruption:bar"
BAR_TOUCHED_KEY = "interruption:bar_touched"

DEFAULT_BAR = 0.6
QUIET_STEP = 0.05
DIG_IN_STEP = 0.10
BAR_CEILING = 0.95
BAR_FLOOR = 0.20
DECAY_PER_DAY = 0.01

MAX_PUSHES_PER_DAY = 3


def bar(reference: Optional[datetime] = None) -> float:
    """The current bar, with decay applied.

    Decay is computed on read rather than written on a schedule: the bar has no
    natural tick, and a value that drifts only when something happens to look
    at it is the same value with less machinery.
    """
    now = reference or datetime.now(timezone.utc)
    value = float(db.get_sync_state(BAR_KEY) or DEFAULT_BAR)
    touched = parse_iso(db.get_sync_state(BAR_TOUCHED_KEY))
    if touched is None:
        return round(value, 4)
    days = max(0.0, (now - touched).total_seconds() / 86400.0)
    if value > DEFAULT_BAR:
        value = max(DEFAULT_BAR, value - DECAY_PER_DAY * days)
    elif value < DEFAULT_BAR:
        value = min(DEFAULT_BAR, value + DECAY_PER_DAY * days)
    return round(value, 4)


def _set_bar(value: float, reference: Optional[datetime] = None) -> float:
    now = reference or datetime.now(timezone.utc)
    value = round(max(BAR_FLOOR, min(BAR_CEILING, value)), 4)
    db.set_sync_state(BAR_KEY, str(value))
    db.set_sync_state(BAR_TOUCHED_KEY, now.isoformat(timespec="seconds"))
    return value


def quieted(reference: Optional[datetime] = None) -> float:
    """"Right thread, wrong moment." Raises the bar: the thread was fine, the
    interruption wasn't."""
    return _set_bar(bar(reference) + QUIET_STEP, reference)


def dug_in(reference: Optional[datetime] = None) -> float:
    """"This matters more than I judged." Lowers the bar — the user is asking
    to be told sooner."""
    return _set_bar(bar(reference) - DIG_IN_STEP, reference)


# ------------------------------------------------------------- the budget
def _pushes_today(reference: Optional[datetime] = None) -> int:
    now = reference or datetime.now(timezone.utc)
    since = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    row = db.get_connection().execute(
        "SELECT COUNT(*) FROM notifications WHERE kind = 'finding' AND created_at >= ?",
        (since,),
    ).fetchone()
    return row[0] if row else 0


def in_quiet_hours(reference: Optional[datetime] = None) -> bool:
    """Nothing below Time-Sensitive pushes outside the waking window. Inherited
    from `config.morning_window` rather than given its own setting — the user
    has already said when their day starts once."""
    now = reference or datetime.now(timezone.utc)
    start, _ = get_config().morning_window
    hour = now.astimezone().hour
    return hour < start or hour >= 22


# How old a finding can be and still be worth breaking into someone's day.
#
# The bar, quiet hours and the daily cap all *defer* a push; nothing ever
# expired one. So a finding written on Saturday morning went out on Monday
# night the moment the cap reset — with its original body, and at its original
# level. On the live database 43 of 60 finding pushes landed more than twelve
# hours after they were written, among them "Assembly deadline is TODAY at
# 1:30 PM (7.5 hours away)" delivered two days after that deadline, and
# "28 hours to deadline" delivered six days after it, both Time-Sensitive,
# both able to be read aloud in a room.
#
# Six hours is the shape of the claim these make. A finding says something
# about the user's day, and a day moves; past that the honest thing is to let
# it wait in the app, where it is still there and no longer shouting.
MAX_PUSH_AGE_HOURS = 6


def _hours_since(iso_ts: Optional[str], now: datetime) -> Optional[float]:
    when = parse_iso(iso_ts)
    return None if when is None else (now - when).total_seconds() / 3600.0


def may_interrupt(finding, reference: Optional[datetime] = None,
                  *, thread=None) -> Dict[str, object]:
    """Should this finding reach the user *right now*?

    Returns the decision and the reason it was made, because a system that
    decides when to interrupt someone should be able to say why it did.

    §v3 (Loose Ends) — **an answer on an end the user added always buzzes.**
    The bar and the daily cap exist to protect the user from the system's own
    judgement about what matters; a loose end they declared *is* their
    judgement, and being told when it moves is the product. Most worker
    answers score exactly 0.50 — a hair under the default bar — and the cap
    ran full for a week straight, so under the old rules the one push the
    user explicitly signed up for was precisely the one that never fired.
    Freshness, once-ever, and quiet hours still apply: an answer is exempt
    from the system's taste, not from the clock.
    """
    now = reference or datetime.now(timezone.utc)
    current = bar(now)

    if finding.kind == "nothing":
        return {"push": False, "reason": "nothing to report", "bar": current}
    if db.notification_exists_for_finding(finding.id):
        return {"push": False, "reason": "already pushed once", "bar": current}
    if getattr(finding, "superseded_at", None):
        return {"push": False, "reason": "superseded by a newer finding",
                "bar": current, "expired": True}
    age = _hours_since(getattr(finding, "created_at", None), now)
    if age is not None and age > MAX_PUSH_AGE_HOURS:
        return {"push": False, "reason": f"too old to interrupt ({age:.0f}h)",
                "bar": current, "expired": True}
    if in_quiet_hours(now):
        return {"push": False, "reason": "quiet hours", "bar": current}
    if thread is not None and thread.origin == ThreadOrigin.USER:
        return {"push": True, "reason": "an answer on an end you added",
                "bar": current}
    if finding.importance < current:
        return {
            "push": False,
            "reason": f"below the bar ({finding.importance:.2f} < {current:.2f})",
            "bar": current,
        }
    if _pushes_today(now) >= MAX_PUSHES_PER_DAY:
        # Not dropped — the briefing and digest lanes still carry it. The cap
        # limits interruption, not delivery.
        return {"push": False, "reason": "daily cap reached", "bar": current}
    return {"push": True, "reason": f"cleared the bar ({finding.importance:.2f})", "bar": current}


ANNOUNCE_IMPORTANCE = 0.85
ANNOUNCE_DEADLINE_HOURS = 24


def level_for(finding, thread, reference: Optional[datetime] = None) -> str:
    """Which APNs interruption level a finding earns.

    Everything used to go out `active`, which is the level Focus silences and
    Announce Notifications skips — so nothing the system found could ever reach
    someone driving, cooking, or wearing AirPods with their phone in a pocket.

    Time-Sensitive is deliberately scarcer than "cleared the bar to push". It
    breaks Focus and can be read aloud in a room with other people in it, so it
    is reserved for the two cases where being early is the whole value: a
    deadline inside a day, or a finding the worker rated near the top.
    """
    now = reference or datetime.now(timezone.utc)

    if finding.importance >= ANNOUNCE_IMPORTANCE:
        return InterruptionLevel.TIME_SENSITIVE

    due = parse_iso(getattr(thread, "deadline", None))
    if due is not None:
        hours = (due - now).total_seconds() / 3600.0
        # Passed counts too — a missed deadline is the most time-sensitive
        # thing the system can know about.
        if hours <= ANNOUNCE_DEADLINE_HOURS:
            return InterruptionLevel.TIME_SENSITIVE

    return InterruptionLevel.ACTIVE


def queue_findings(reference: Optional[datetime] = None) -> List[str]:
    """Push what has earned it. Called from the notification pass."""
    now = reference or datetime.now(timezone.utc)
    sent: List[str] = []

    for finding in db.unsurfaced_findings():
        thread = db.get_thread(finding.thread_id)
        if thread is None:
            continue
        verdict = may_interrupt(finding, now, thread=thread)
        if not verdict["push"]:
            if verdict.get("expired"):
                # Retire it from the push queue rather than leaving it to
                # drain at midnight. The finding is untouched on its thread —
                # this only ends its claim on an interruption.
                db.mark_finding_surfaced(finding.id)
                log.info("expired finding %r (%s)", finding.headline[:60], verdict["reason"])
            continue
        db.queue_notification(
            "finding",
            level_for(finding, thread, now),
            thread.title,
            finding.headline,
            finding_id=finding.id,
        )
        db.mark_finding_surfaced(finding.id)
        sent.append(finding.id)
        log.info("pushed finding %r (%s)", finding.headline[:60], verdict["reason"])
    return sent


def state(reference: Optional[datetime] = None) -> Dict[str, object]:
    """What the budget looks like right now — for /health and for the user."""
    now = reference or datetime.now(timezone.utc)
    return {
        "bar": bar(now),
        "default_bar": DEFAULT_BAR,
        "pushes_today": _pushes_today(now),
        "daily_cap": MAX_PUSHES_PER_DAY,
        "quiet_hours": in_quiet_hours(now),
    }
