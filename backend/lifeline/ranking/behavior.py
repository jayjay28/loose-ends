"""Avoidance vs. deprioritization (§6.3).

Both look identical in the data at first glance — an item that keeps not
getting done — but they demand opposite responses: surface *more* insistently
versus stop surfacing at all. Getting this backwards is the worst thing the
product can do, so the classifier requires corroboration across the four
signals in the Section 6.3 table rather than firing on view count alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .. import db
from ..extraction.dates import days_until
from ..models import BehaviorPattern, Item, parse_iso
from .signals import immediacy_ratio

VIEW_THRESHOLD = 3          # "opened repeatedly (3+), no action"
DEADLINE_CLUSTER_DAYS = 2.0  # action clustering right before a deadline


@dataclass
class BehaviorReading:
    pattern: Optional[str] = None
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)

    @property
    def is_avoidance(self) -> bool:
        return self.pattern == BehaviorPattern.AVOIDANCE

    @property
    def is_deprioritized(self) -> bool:
        return self.pattern == BehaviorPattern.DEPRIORITIZED


def _view_count(item: Item) -> int:
    counts = db.behavior_counts(item.id)
    return counts.get("viewed", 0) + counts.get("expanded", 0)


def _acted(item: Item) -> bool:
    counts = db.behavior_counts(item.id)
    return any(counts.get(k) for k in ("acted", "completed_manual", "completed_auto", "replied"))


def deadline_clustering(person_id: Optional[str], item_type: Optional[str]) -> Tuple[bool, int]:
    """Does this user historically act on this kind of item only at the wire?

    Anxiety-driven relief-seeking shows up as completions bunched into the
    last couple of days before a deadline — evidence of avoidance, not of
    indifference.
    """
    rows = db.behavior_events(kinds=["completed_manual", "acted"], person_id=person_id, item_type=item_type)
    near, total = 0, 0
    for row in rows:
        if not row["item_id"]:
            continue
        item = db.get_item(row["item_id"])
        if not item or not item.entities.date:
            continue
        occurred = parse_iso(row["occurred_at"])
        if not occurred:
            continue
        remaining = days_until(item.entities.date, occurred)
        if remaining is None:
            continue
        total += 1
        if -0.5 <= remaining <= DEADLINE_CLUSTER_DAYS:
            near += 1
    return (total >= 2 and near / total >= 0.6), total


def cross_context(item: Item) -> Tuple[Optional[str], str]:
    """§6.3 row 3 — compare this item against the user's behaviour on the same
    sender's *other* items, and on this item type across *all* senders."""
    if not item.person_id:
        return None, ""

    sender_ratio, sender_n = _sender_action_ratio(item.person_id, exclude_item_id=item.id)
    type_ratio, type_n = _type_action_ratio(item.type)

    if sender_n >= 2 and sender_ratio >= 0.6:
        return (
            BehaviorPattern.AVOIDANCE,
            f"you act on {int(sender_ratio * 100)}% of {item.person}'s other items but not this one",
        )
    if type_n >= 3 and type_ratio <= 0.2:
        return (
            BehaviorPattern.DEPRIORITIZED,
            f"you act on only {int(type_ratio * 100)}% of {item.type} items from anyone",
        )
    return None, ""


def _sender_action_ratio(person_id: str, exclude_item_id: Optional[str] = None) -> Tuple[float, int]:
    items = [i for i in db.list_items(person_id=person_id) if i.id != exclude_item_id]
    closed = [i for i in items if i.status == "completed"]
    considered = [i for i in items if i.status in ("completed", "pending", "snoozed", "dismissed")]
    if not considered:
        return 0.0, 0
    return len(closed) / len(considered), len(considered)


def _type_action_ratio(item_type: str) -> Tuple[float, int]:
    items = [i for i in db.list_items() if i.type == item_type]
    if not items:
        return 0.0, 0
    closed = [i for i in items if i.status == "completed"]
    return len(closed) / len(items), len(items)


def classify(item: Item, reference: Optional[datetime] = None) -> BehaviorReading:
    """Read an un-actioned item as avoidance, deprioritization, or neither."""
    if item.status not in ("pending", "snoozed") or _acted(item):
        return BehaviorReading()

    now = reference or datetime.now(timezone.utc)
    created = parse_iso(item.created_at) or now
    age_days = (now - created).total_seconds() / 86400.0
    views = _view_count(item)

    avoidance_evidence: List[str] = []
    deprioritized_evidence: List[str] = []

    # Row 1 — view count.
    if views >= VIEW_THRESHOLD:
        avoidance_evidence.append(f"opened {views} times without acting")
    elif views == 1 and age_days >= 7:
        deprioritized_evidence.append("opened once a week ago and never revisited")
    elif views == 0 and age_days >= 14:
        deprioritized_evidence.append("never opened in two weeks")

    # Row 2 — deadline proximity effect.
    clusters, sample = deadline_clustering(item.person_id, item.type)
    if clusters:
        avoidance_evidence.append("you historically act on these only right before the deadline")
    elif sample >= 3:
        deprioritized_evidence.append("no deadline effect in your history with these")

    # Row 3 — cross-context comparison.
    pattern, note = cross_context(item)
    if pattern == BehaviorPattern.AVOIDANCE:
        avoidance_evidence.append(note)
    elif pattern == BehaviorPattern.DEPRIORITIZED:
        deprioritized_evidence.append(note)

    # Row 4 — emotional weight of the task.
    from .signals import emotional_weight

    weight, detail = emotional_weight(item)
    if weight > 0 and views >= 2:
        avoidance_evidence.append(detail)
    elif weight == 0 and views <= 1 and age_days >= 7:
        deprioritized_evidence.append("low-stakes and untouched")

    # Immediacy history is a tiebreaker, not a verdict on its own.
    if item.person_id:
        ratio, seen = immediacy_ratio(item.person_id, item.type)
        if seen >= 3 and ratio <= 0.1:
            deprioritized_evidence.append("never acted on when surfaced")

    # Require corroboration: one weak signal is not a diagnosis.
    if len(avoidance_evidence) >= 2 and len(avoidance_evidence) > len(deprioritized_evidence):
        return BehaviorReading(
            BehaviorPattern.AVOIDANCE, min(0.5 + 0.15 * len(avoidance_evidence), 0.95), avoidance_evidence
        )
    if len(deprioritized_evidence) >= 2 and len(deprioritized_evidence) > len(avoidance_evidence):
        return BehaviorReading(
            BehaviorPattern.DEPRIORITIZED,
            min(0.5 + 0.15 * len(deprioritized_evidence), 0.95),
            deprioritized_evidence,
        )
    return BehaviorReading(None, 0.0, avoidance_evidence + deprioritized_evidence)
