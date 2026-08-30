"""Importance scoring and interruption-level assignment (§6, milestone 4).

A weighted sum over the Section 6.2 signals — deliberately linear so that
every point of score is attributable to a named signal, which is what makes
the "why this is ranked here" explanation in §8.3 honest rather than
post-hoc.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .. import db
from ..extraction.dates import days_until
from ..models import BehaviorPattern, InterruptionLevel, Item
from . import behavior, signals

# Static weights (milestone 4). The learning loop adjusts the *inputs*
# (person/type/pair weights); these blend coefficients stay fixed so the
# model can't run away from itself.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "deadline_pressure": 2.4,
    "dependency_pressure": 1.8,
    "sender_weight": 2.0,
    "type_weight": 1.2,
    "pair_weight": 0.9,
    "response_latency": 0.8,
    "action_immediacy": 1.0,
    "explicit_signals": 0.7,
    "language_tone": 0.9,
    "emotional_weight": 0.4,
    "staleness": 1.4,
    "currency": 1.8,
    "unread": 1.0,
    "time_of_day": 0.6,
}

TIME_SENSITIVE_DAYS = 3.0
PASSIVE_SCORE_CEILING = 1.2
TIME_SENSITIVE_SCORE_FLOOR = 3.6

# §6.3: avoidance is surfaced *gently*. A large boost would be spam.
AVOIDANCE_BOOST = 0.6
DEPRIORITIZATION_PENALTY = 1.1


def weight(name: str) -> float:
    return db.get_weight(f"signal:{name}", DEFAULT_WEIGHTS.get(name, 1.0))


def _collect(item: Item, reference: Optional[datetime]) -> List[Tuple[str, float, str]]:
    return [
        ("deadline_pressure", *signals.deadline_pressure(item, reference)),
        ("dependency_pressure", *signals.dependency_pressure(item, reference)),
        ("sender_weight", *signals.sender_weight(item)),
        ("type_weight", *signals.type_weight(item)),
        ("pair_weight", *signals.pair_weight(item)),
        ("response_latency", *signals.response_latency(item)),
        ("action_immediacy", *signals.action_immediacy(item)),
        ("explicit_signals", *signals.explicit_signals(item)),
        ("language_tone", *signals.language_tone(item)),
        ("emotional_weight", *signals.emotional_weight(item)),
        ("staleness", *signals.staleness(item, reference)),
        ("currency", *signals.currency(item, reference)),
        ("unread", *signals.unread(item, reference)),
        ("time_of_day", *signals.time_of_day(item, reference)),
    ]


def score_item(item: Item, reference: Optional[datetime] = None) -> Item:
    """Compute score, interruption level and explanation. Does not persist."""
    now = reference or datetime.now(timezone.utc)
    explanation: List[Dict[str, Any]] = []
    total = 0.0

    for name, value, detail in _collect(item, now):
        w = weight(name)
        contribution = round(value * w, 3)
        total += contribution
        if abs(contribution) >= 0.05:
            explanation.append(
                {"signal": name, "value": round(value, 3), "weight": w, "contribution": contribution, "detail": detail}
            )

    reading = behavior.classify(item, now)
    if reading.is_avoidance:
        total += AVOIDANCE_BOOST
        explanation.append(
            {
                "signal": "avoidance",
                "value": reading.confidence,
                "weight": AVOIDANCE_BOOST,
                "contribution": AVOIDANCE_BOOST,
                "detail": "looks like avoidance, not indifference: " + "; ".join(reading.evidence),
            }
        )
    elif reading.is_deprioritized:
        total -= DEPRIORITIZATION_PENALTY
        explanation.append(
            {
                "signal": "deprioritized",
                "value": reading.confidence,
                "weight": -DEPRIORITIZATION_PENALTY,
                "contribution": -DEPRIORITIZATION_PENALTY,
                "detail": "you've consistently passed on these: " + "; ".join(reading.evidence),
            }
        )

    item.score = round(total, 3)
    item.behavior_pattern = reading.pattern
    item.interruption_level = assign_level(item, reading, now)
    explanation.sort(key=lambda e: abs(e["contribution"]), reverse=True)
    item.score_explanation = explanation
    return item


def assign_level(item: Item, reading: behavior.BehaviorReading, reference: Optional[datetime] = None) -> str:
    """§6.1 — Time-Sensitive / Active / Passive."""
    now = reference or datetime.now(timezone.utc)
    days = days_until(item.entities.date, now)
    dependency, _ = signals.dependency_pressure(item, now)

    # Time-Sensitive: a hard deadline is arriving, or dependent work is
    # already inside its lead time.
    if days is not None and -0.5 <= days <= TIME_SENSITIVE_DAYS:
        return InterruptionLevel.TIME_SENSITIVE
    if dependency >= 0.9:
        return InterruptionLevel.TIME_SENSITIVE
    if item.score >= TIME_SENSITIVE_SCORE_FLOOR and days is not None and days <= 7:
        return InterruptionLevel.TIME_SENSITIVE

    # Passive: explicitly flexible, learned-low, or simply low-stakes. These
    # batch into a collapsed summary rather than being surfaced one by one.
    tone, _ = signals.language_tone(item)
    if reading.is_deprioritized:
        return InterruptionLevel.PASSIVE
    # "no rush" demotes an open-ended ask, but it cannot override a date the
    # sender also put on it.
    if tone < 0 and days is None:
        return InterruptionLevel.PASSIVE
    if item.score < PASSIVE_SCORE_CEILING:
        return InterruptionLevel.PASSIVE
    if item.type == "reading" and days is None and item.score < 2.2:
        return InterruptionLevel.PASSIVE

    return InterruptionLevel.ACTIVE


def rescore_all(reference: Optional[datetime] = None) -> int:
    """Re-rank every open item. Cheap enough to run after any state change."""
    items = db.open_items()
    for item in items:
        score_item(item, reference)
    db.save_items(items)
    return len(items)


def ranked(reference: Optional[datetime] = None, include_snoozed: bool = False) -> List[Item]:
    """Open items, highest first, with snoozed items filtered until they wake."""
    now = reference or datetime.now(timezone.utc)
    out = []
    for item in db.open_items():
        if item.status == "snoozed" and not include_snoozed:
            wake = item.snoozed_until
            if wake and (days_until(wake, now) or 0) > 0:
                continue
        out.append(item)
    out.sort(key=lambda i: (InterruptionLevel.ORDER.get(i.interruption_level, 1), -i.score))
    return out
