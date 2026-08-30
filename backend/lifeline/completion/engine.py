"""Completion detection engine (§7, milestones 5 and 8).

Closes the loop without a manual mark-done. Runs after every ingest:

    1. new Gmail/Calendar data arrives
    2. every pending item is matched against it
    3. high confidence  -> auto-close, positive notification, signal logged
    4. low confidence   -> "possible match, confirm?" (never auto-closed)
    5. no signal        -> left alone, subject to re-ranking

Manual override is always available, and a manual close teaches the model
that this item type doesn't reliably produce an external signal.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .. import db
from ..models import CompletionSignal, Item, new_id, now_iso, parse_iso
from ..ranking import learning
from . import matcher

log = logging.getLogger(__name__)

# How far back to look for evidence when an item is first scanned.
EVIDENCE_WINDOW_DAYS = 45

# §7 — iMessage self-reply close. For loop-closing types, the user's own reply
# in the same thread is the completion signal. Kept to types where a reply
# genuinely closes the loop, and to a window after the item — replying within
# two weeks means you handled it; a promise or a reading isn't "done" just
# because you said something later, and a reply months later is too ambiguous
# to auto-close on (recency decay already demotes those).
SELF_REPLY_TYPES = {"question", "followup"}
SELF_REPLY_CLOSE_DAYS = 14.0

# A reply that promises to answer is not an answer. "Let me check and get back
# to you" is the loop being *held open*, in writing, and it was closing the
# item at 0.9 — which then closed the thread that claimed it, because every
# item being settled is the thread's strongest closure signal. The one shape
# of reply the rule above cannot be allowed to read as done.
_DEFERRAL = re.compile(
    r"\b(i'?ll (?:check|look|ask|find out|see|get back|let you know)|"
    r"let me (?:check|look|see|ask|find out|get back)|"
    r"get(?:ting)? back to you|i'?ll let you know|not sure yet|"
    r"will do|on it|gimme a (?:sec|min|minute)|one sec)\b",
    re.I,
)


def _self_reply_signal(item: Item, now: datetime) -> Optional[CompletionSignal]:
    """The user's own answer, if they gave one.

    Reads past a deferral rather than stopping at the first thing they said:
    "let me check" then, an hour later, "2pm" is a question that was answered,
    and taking only the first reply would leave it open forever.
    """
    if item.type not in SELF_REPLY_TYPES or not item.conversation_id:
        return None
    asked = parse_iso(item.timestamp)
    if not asked:
        return None

    for reply in db.user_replies_after(item.conversation_id, item.timestamp):
        if db.signal_exists(item.id, reply.id):
            return None
        replied = parse_iso(reply.timestamp)
        if not replied:
            continue
        if (replied - asked).total_seconds() > SELF_REPLY_CLOSE_DAYS * 86400:
            return None      # ordered by time: everything after this is later still
        if _DEFERRAL.search(reply.text or ""):
            continue         # a promise to answer is not an answer
        snippet = reply.text.strip().replace("\n", " ")[:60]
        return CompletionSignal(
            id=new_id(),
            item_id=item.id,
            source="imessage",
            evidence_ref=reply.id,
            evidence_text=reply.text[:200],
            confidence=0.9,
            reasons=[f'you replied "{snippet}" after this'],
            resolution="auto_closed",
            detected_at=now_iso(),
        )
    return None


@dataclass
class Outcome:
    auto_closed: List[Item] = field(default_factory=list)
    needs_confirmation: List[CompletionSignal] = field(default_factory=list)
    scanned: int = 0

    def summary(self) -> Dict[str, int]:
        return {
            "scanned": self.scanned,
            "auto_closed": len(self.auto_closed),
            "needs_confirmation": len(self.needs_confirmation),
        }


def _threshold_for(item: Item) -> float:
    """Types that rarely produce external evidence get a slightly higher bar
    for auto-closing — a weak match on "call the pediatrician" should ask,
    not assume (§7, and the manual-close feedback in §6)."""
    manual_rate = learning.manual_close_rate(item.type)
    return matcher.AUTO_CLOSE + 0.1 * max(0.0, manual_rate - 0.5) * 2


def _evidence_floor(item: Item) -> str:
    """Earliest timestamp at which evidence could count for this item.

    Normally the item's own timestamp — you can't be closed by something that
    happened before you were asked. Follow-ups are the exception: "did you
    ever book the flights?" refers back to an earlier commitment, and the
    booking confirmation may well pre-date the nudge.
    """
    if item.type != "followup":
        return item.timestamp
    if item.links_to_item_id:
        original = db.get_item(item.links_to_item_id)
        if original:
            return original.timestamp
    anchor = parse_iso(item.timestamp)
    if not anchor:
        return item.timestamp
    return (anchor - timedelta(days=EVIDENCE_WINDOW_DAYS)).isoformat(timespec="seconds")


def _best_match(item: Item, reference: Optional[datetime] = None) -> Optional[matcher.Match]:
    best: Optional[matcher.Match] = None
    floor = _evidence_floor(item)

    for message in db.messages_since(floor, source="gmail"):
        if db.signal_exists(item.id, message.id):
            continue
        candidate = matcher.match_email(item, message, evidence_floor=floor)
        if best is None or candidate.confidence > best.confidence:
            best = candidate

    for event in db.list_calendar_events():
        if db.signal_exists(item.id, event.id):
            continue
        candidate = matcher.match_calendar(item, event, reference)
        if best is None or candidate.confidence > best.confidence:
            best = candidate

    return best if best and best.confidence > 0 else None


def close_item(item: Item, by: str, signal: Optional[CompletionSignal] = None) -> Item:
    """Mark an item complete and feed the outcome back into the model."""
    item.status = "completed"
    item.completed_at = now_iso()
    item.completed_by = by
    db.save_item(item)
    learning.record("completed_auto" if by == "auto" else "completed_manual", item, payload={
        "signal_id": signal.id if signal else None,
        "source": signal.source if signal else None,
    })
    return item


def scan(reference: Optional[datetime] = None) -> Outcome:
    """Match every open item against all available evidence."""
    now = reference or datetime.now(timezone.utc)
    outcome = Outcome()

    for item in db.open_items():
        outcome.scanned += 1

        # A reply from the user in the same thread closes loop-closing items —
        # the iMessage-native completion signal (Gmail/Calendar matching below
        # only ever fired for those sources, so iMessage items never closed).
        self_reply = _self_reply_signal(item, now)
        if self_reply is not None:
            from ..notifications import scheduler   # local: avoids an import cycle

            self_reply.resolved_at = now_iso()
            db.save_signal(self_reply)
            close_item(item, by="auto", signal=self_reply)
            scheduler.queue_completion(item, evidence="; ".join(self_reply.reasons)[:180])
            outcome.auto_closed.append(item)
            log.info("auto-closed %s via self-reply", item.id)
            continue

        match = _best_match(item, now)
        if not match:
            continue

        resolution = match.resolution
        if resolution is None:
            continue
        if resolution == "auto_closed" and match.confidence < _threshold_for(item):
            resolution = "needs_confirmation"

        signal = CompletionSignal(
            id=new_id(),
            item_id=item.id,
            source=match.source,
            evidence_ref=match.evidence_ref,
            evidence_text=match.evidence_text,
            confidence=match.confidence,
            reasons=match.reasons,
            resolution=resolution,
            detected_at=now_iso(),
        )

        if resolution == "auto_closed":
            from ..notifications import scheduler   # local: avoids an import cycle

            signal.resolved_at = now_iso()
            db.save_signal(signal)
            close_item(item, by="auto", signal=signal)
            # §7: notify with a positive message, and say what closed it.
            scheduler.queue_completion(item, evidence="; ".join(match.reasons)[:180])
            outcome.auto_closed.append(item)
            log.info("auto-closed %s via %s (%.2f)", item.id, match.source, match.confidence)
        else:
            # §7 step 4 — never auto-close a fuzzy match; ask.
            db.save_signal(signal)
            outcome.needs_confirmation.append(signal)

    return outcome


# --------------------------------------------------- confirm-to-close (M8)
def confirm(signal_id: str) -> Optional[Item]:
    """User accepted a fuzzy match. Close the item and reinforce the matcher."""
    signal = db.get_signal(signal_id)
    if not signal or signal.resolution not in ("needs_confirmation",):
        return None
    item = db.get_item(signal.item_id)
    if not item:
        return None
    signal.resolution = "confirmed"
    signal.resolved_at = now_iso()
    db.save_signal(signal)
    close_item(item, by="auto", signal=signal)
    db.log_behavior("confirmed_match", item_id=item.id, payload={"signal_id": signal.id})
    return item


def reject(signal_id: str) -> Optional[Item]:
    """User said no. Keep the item open and remember not to re-suggest this
    piece of evidence."""
    signal = db.get_signal(signal_id)
    if not signal:
        return None
    signal.resolution = "rejected"
    signal.resolved_at = now_iso()
    db.save_signal(signal)
    item = db.get_item(signal.item_id)
    if item:
        db.log_behavior("rejected_match", item_id=item.id, payload={"signal_id": signal.id})
    return item


def manual_close(item_id: str) -> Optional[Item]:
    """§7 — manual override is always available, and it teaches the model."""
    item = db.get_item(item_id)
    if not item:
        return None
    # Any outstanding suggestion for this item is now moot.
    for signal in db.signals_for_item(item.id):
        if signal.resolution == "needs_confirmation":
            signal.resolution = "rejected"
            signal.resolved_at = now_iso()
            db.save_signal(signal)
    return close_item(item, by="manual")


def open_confirmations() -> List[Dict[str, object]]:
    """Fuzzy matches awaiting a yes/no, newest first."""
    out = []
    for signal in db.pending_confirmations():
        item = db.get_item(signal.item_id)
        if not item or item.status == "completed":
            continue
        out.append(
            {
                "signal_id": signal.id,
                "item": item,
                "source": signal.source,
                "confidence": signal.confidence,
                "evidence_text": signal.evidence_text,
                "reasons": signal.reasons,
                "detected_at": signal.detected_at,
            }
        )
    return out
