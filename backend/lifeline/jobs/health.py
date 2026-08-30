"""The doctor, on a schedule, speaking only when the answer changes.

`lifeline doctor` has been able to name every one of these failures in four
seconds since the day it was written. Nothing ran it. So iMessage returned zero
messages on 124 consecutive poll cycles and Gmail went thirteen days without
ingesting, and both were discovered by a person eventually wondering, rather
than by the system saying so.

A check nobody calls is a check that does not exist. This is the caller.

Three rules, each answering a way this could go wrong instead:

**It speaks on change, not on state.** Notifying every cycle that iMessage is
still broken trains you to swipe it away, and a notification you have learned
to ignore is worse than none — it is the silence again, with extra steps. A
failure announces itself when it appears, once more if it is still there a day
later, and once when it clears.

**It does not run every cycle.** The doctor asks real questions: it refreshes
the Google token, opens `chat.db`, and asks both models to generate. That is a
few network calls and a fraction of a cent, which is nothing hourly and is
waste every thirty minutes for an answer that does not change that fast.

**It cannot break the poll.** A cycle that dies inside its own health check
would be the funniest possible version of this bug, so the whole thing is
wrapped and a failure to check is logged, not raised.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .. import db
from ..models import InterruptionLevel, parse_iso

log = logging.getLogger(__name__)

KIND = "health"

LAST_RUN_KEY = "doctor:last_run"
FAILING_KEY = "doctor:failing"
LAST_ALERT_KEY = "doctor:last_alert"

# The doctor's answers move on the scale of hours — a key runs out, a
# permission is revoked, a token stops refreshing. Hourly catches all of it.
MIN_INTERVAL_HOURS = 1.0

# Still broken a day later is worth saying once more. Still broken an hour
# later is not: you already know, and you have not had time to fix it.
RENOTIFY_HOURS = 24.0


def _hours_since(key: str, reference: datetime) -> float:
    last = parse_iso(db.get_sync_state(key))
    if not last:
        return float("inf")
    return (reference - last).total_seconds() / 3600.0


def _summarise(checks: List[Any]) -> tuple:
    """One failure names itself; several are counted and listed.

    The fix line rides along in the body because the notification arrives on a
    phone, away from the terminal that would otherwise have to be asked.
    """
    if len(checks) == 1:
        c = checks[0]
        title = f"Lifeline: {c.name} stopped working"
        body = c.detail if not c.fix else f"{c.detail} — {c.fix}"
    else:
        names = ", ".join(c.name for c in checks)
        title = f"Lifeline: {len(checks)} checks failing"
        body = f"{names}. {checks[0].detail}"
    return title, body[:180]


def run(reference: Optional[datetime] = None, force: bool = False) -> Dict[str, Any]:
    """Ask the doctor, and notify only if the answer is news.

    `force` is for the CLI and the tests — the interval gate exists to keep the
    poll cheap, not to stop a person asking.
    """
    now = reference or datetime.now(timezone.utc)
    if not force and _hours_since(LAST_RUN_KEY, now) < MIN_INTERVAL_HOURS:
        return {"ran": False, "reason": "checked within the last hour"}

    from .. import doctor

    report = doctor.run()
    db.set_sync_state(LAST_RUN_KEY, now.isoformat(timespec="seconds"))

    failing = sorted(c.name for c in report.failed)
    previous = [n for n in (db.get_sync_state(FAILING_KEY) or "").split(",") if n]
    db.set_sync_state(FAILING_KEY, ",".join(failing))

    appeared = [n for n in failing if n not in previous]
    # Still broken, and last said so a day ago or more.
    overdue = bool(failing) and _hours_since(LAST_ALERT_KEY, now) >= RENOTIFY_HOURS
    recovered = bool(previous) and not failing

    result: Dict[str, Any] = {
        "ran": True,
        "failing": failing,
        "warning": [c.name for c in report.warned],
        "notified": None,
    }

    if appeared or overdue:
        # Every failure goes in the message, not only the new one — the phone
        # should carry the whole picture, and a second broken thing arriving
        # while the first is unfixed must not hide the first.
        title, body = _summarise(report.failed)
        # Logged as well as pushed. The push needs a registered device and a
        # working APNs key, neither of which is guaranteed at exactly the
        # moment the system is coming apart; the log needs nothing.
        log.warning("health: %s — %s", title, body)
        db.queue_notification(KIND, InterruptionLevel.ACTIVE, title, body)
        db.set_sync_state(LAST_ALERT_KEY, now.isoformat(timespec="seconds"))
        result["notified"] = "failing"

    elif recovered:
        # Worth saying out loud. Without it the last thing you were ever told
        # is that the system was broken, and you have to go and check to learn
        # otherwise — which is the habit this module exists to remove.
        fixed = ", ".join(previous)
        log.warning("health: recovered — %s", fixed)
        db.queue_notification(
            KIND, InterruptionLevel.PASSIVE,
            "Lifeline is working again", f"{fixed} recovered.",
        )
        db.set_sync_state(LAST_ALERT_KEY, now.isoformat(timespec="seconds"))
        result["notified"] = "recovered"

    return result


def run_safely(reference: Optional[datetime] = None) -> Dict[str, Any]:
    """The poll cycle's entry point. Never raises."""
    try:
        return run(reference)
    except Exception as exc:                       # noqa: BLE001
        log.warning("health check failed to run: %s", exc, exc_info=True)
        return {"ran": False, "error": str(exc)[:200]}
