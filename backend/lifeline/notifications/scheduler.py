"""What to notify about, and when (§8.4, milestone 9).

The rules are deliberately restrictive — an app that surfaces forgotten
obligations earns its place by being quiet:

  * Time-Sensitive  — pushes once, immediately, and may break through Focus.
  * Active          — never pushes on its own; folded into one morning
                      briefing inside the §6.4 mid-morning window.
  * Passive         — never pushes individually, ever. One batched digest.
  * Completions     — celebratory but low-priority, and batched the same way.

Every notification is recorded, and nothing is sent for an item twice.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .. import db
from ..config import get_config
from ..models import InterruptionLevel, Item, ThreadState
from ..ranking import scorer
from . import apns

log = logging.getLogger(__name__)

BRIEFING_KEY = "notifications:last_briefing"
DIGEST_KEY = "notifications:last_digest"
DIGEST_MIN_ITEMS = 2
DIGEST_INTERVAL_HOURS = 24


def _today(reference: datetime) -> str:
    return reference.astimezone().strftime("%Y-%m-%d")


def _hours_since(key: str, reference: datetime) -> float:
    from ..models import parse_iso

    last = parse_iso(db.get_sync_state(key))
    if not last:
        return float("inf")
    return (reference - last).total_seconds() / 3600.0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ------------------------------------------------------------ time-sensitive
def queue_time_sensitive(reference: Optional[datetime] = None) -> List[str]:
    """One push per Time-Sensitive item, the first time it reaches that level."""
    queued = []
    for item in scorer.ranked(reference):
        if item.interruption_level != InterruptionLevel.TIME_SENSITIVE:
            continue
        if db.notification_exists_for_item(item.id, "item"):
            continue
        title = f"{item.person} — {item.type}"
        body = item.suggested_action or item.raw_text[:120]
        queued.append(db.queue_notification("item", InterruptionLevel.TIME_SENSITIVE, title, body, item.id))
        db.log_behavior("surfaced", item_id=item.id, payload={"channel": "push"})
    return queued


# ------------------------------------------------------------------ briefing
def queue_morning_briefing(reference: Optional[datetime] = None) -> Optional[str]:
    """§6.4 — one briefing in the mid-morning window, weighted to items that
    require a decision. Active items ride along here rather than pushing."""
    now = reference or datetime.now(timezone.utc)
    cfg = get_config()
    local_hour = now.astimezone().hour
    if not (cfg.morning_window[0] <= local_hour < cfg.morning_window[1]):
        return None
    if db.get_sync_state(BRIEFING_KEY) == _today(now):
        return None

    ranked = scorer.ranked(now)
    decisions = [i for i in ranked if i.interruption_level in (InterruptionLevel.TIME_SENSITIVE, InterruptionLevel.ACTIVE)]
    if not decisions:
        return None

    urgent = [i for i in decisions if i.interruption_level == InterruptionLevel.TIME_SENSITIVE]
    lead = decisions[0]
    if urgent:
        title = f"{_plural(len(urgent), 'thing')} needs you today"
    else:
        title = f"{_plural(len(decisions), 'thing')} to decide"
    body = lead.suggested_action or lead.raw_text[:120]
    if len(decisions) > 1:
        body += f" · and {len(decisions) - 1} more"

    db.set_sync_state(BRIEFING_KEY, _today(now))
    for item in decisions:
        db.log_behavior("surfaced", item_id=item.id, payload={"channel": "briefing"})
    return db.queue_notification("briefing", InterruptionLevel.ACTIVE, title, body)


# -------------------------------------------------------------------- digest
def queue_passive_digest(reference: Optional[datetime] = None) -> Optional[str]:
    """§8.4 — Passive items batch into a single periodic summary, never one
    notification per item."""
    now = reference or datetime.now(timezone.utc)
    if _hours_since(DIGEST_KEY, now) < DIGEST_INTERVAL_HOURS:
        return None

    passive = [i for i in scorer.ranked(now) if i.interruption_level == InterruptionLevel.PASSIVE]
    fresh = [i for i in passive if not db.notification_exists_for_item(i.id, "digest")]
    if len(fresh) < DIGEST_MIN_ITEMS:
        return None

    people = sorted({i.person for i in fresh})
    who = ", ".join(people[:3]) + (f" and {len(people) - 3} more" if len(people) > 3 else "")
    title = f"{_plural(len(fresh), 'low-key item')} waiting"
    body = f"From {who}. Nothing urgent — whenever you have a minute."

    notification_id = db.queue_notification("passive_digest", InterruptionLevel.PASSIVE, title, body)
    for item in fresh:
        # Recorded per item so the digest never repeats itself.
        db.queue_notification("digest", InterruptionLevel.PASSIVE, title, body, item.id)
        db.log_behavior("surfaced", item_id=item.id, payload={"channel": "digest"})
    db.set_sync_state(DIGEST_KEY, now.isoformat(timespec="seconds"))
    return notification_id


# ---------------------------------------------------------------- completion
_CELEBRATIONS = [
    "Closed itself — {action}",
    "Done without you lifting a finger: {action}",
    "Handled: {action}",
    "That's off the list: {action}",
]


def queue_completion(item: Item, evidence: str = "") -> Optional[str]:
    """§7/§8.3 — celebratory, non-intrusive feedback on auto-detected closes."""
    if db.notification_exists_for_item(item.id, "completion"):
        return None
    action = item.suggested_action or item.raw_text[:80]
    template = _CELEBRATIONS[hash(item.id) % len(_CELEBRATIONS)]
    title = template.format(action=action[:70])
    body = evidence or "Detected from your email and calendar."
    return db.queue_notification("completion", InterruptionLevel.PASSIVE, title, body, item.id)


# ------------------------------------------------------------------ delivery
def flush(reference: Optional[datetime] = None) -> Dict[str, int]:
    """Send everything queued. Passive-level payloads are delivered silently."""
    devices = db.list_devices()
    sent, skipped = 0, 0

    for row in db.unsent_notifications():
        # A finding always belongs to a thread; that is the screen the tap
        # should open.
        thread_id = None
        if row["finding_id"]:
            finding = db.get_finding(row["finding_id"])
            thread_id = finding.thread_id if finding else None
        if thread_id is None and row["item_id"]:
            # Item-scoped rows (completions) have no finding, but the item is
            # usually claimed as evidence by a thread — that thread is where
            # the tap lands. Prefer a thread the user still carries; a
            # resolved one (the item's completion may be what closed it) still
            # beats no destination at all.
            claiming = db.threads_claiming("item", row["item_id"])
            carried = [t for t in claiming if t.state in ThreadState.OPEN]
            pick = carried or claiming
            if pick:
                thread_id = max(pick, key=lambda t: t.updated_at).id
        payload = apns.build_payload(
            title=row["title"],
            body=row["body"],
            interruption=row["interruption"],
            item_id=row["item_id"],
            conversation_id=row["kind"],
            thread_id=thread_id,
            finding_id=row["finding_id"],
        )
        if row["kind"] == "digest":
            # Per-item digest rows exist only as bookkeeping; the single
            # `passive_digest` row is the one that actually gets delivered.
            db.mark_notification_sent(row["id"])
            skipped += 1
            continue
        if devices:
            from . import relay

            if get_config().has_apns or not relay.configured():
                # Our own key: push directly, words and all. (No key and no
                # relay falls through here too — apns.send logs the dry run.)
                apns.send(devices, payload, collapse_id=row["kind"])
            else:
                # No key — knock through the relay: ids only, the phone
                # fetches the words from this engine (§v3 workstream 5).
                relay.send(
                    devices,
                    level=apns.APNS_LEVEL.get(row["interruption"], "active"),
                    collapse_id=row["kind"],
                    thread_id=thread_id,
                    finding_id=row["finding_id"],
                )
        db.mark_notification_sent(row["id"])
        sent += 1

    return {"sent": sent, "bookkeeping": skipped, "devices": len(devices)}


def run(reference: Optional[datetime] = None) -> Dict[str, object]:
    """Full notification pass — safe to call on every poll cycle."""
    now = reference or datetime.now(timezone.utc)
    from . import interruption

    return {
        "findings": len(interruption.queue_findings(now)),
        "time_sensitive": len(queue_time_sensitive(now)),
        "briefing": bool(queue_morning_briefing(now)),
        "digest": bool(queue_passive_digest(now)),
        "delivery": flush(now),
    }
