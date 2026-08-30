"""Adaptive presentation (§8.1).

Per the CAVE model the interface senses context and responds, rather than
rendering one fixed dashboard. The decision of *what shape today has* is made
here, on the server, where the full item mix and the learned model live; the
client renders the shape it is handed.

Three things drive it: time of day, day of week, and the current item mix.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..models import InterruptionLevel, Item
from .schemas import GroupOut, ItemOut

SURGE_THRESHOLD = 5      # "five Time-Sensitive items compress everything else"


def _bucket(items: List[Item]) -> Dict[str, List[Item]]:
    buckets: Dict[str, List[Item]] = {level: [] for level in InterruptionLevel.ALL}
    for item in items:
        buckets.setdefault(item.interruption_level, []).append(item)
    return buckets


def _mode(items: List[Item], now: datetime) -> Tuple[str, str, Optional[str]]:
    hour = now.astimezone().hour
    weekend = now.astimezone().weekday() >= 5
    urgent = [i for i in items if i.interruption_level == InterruptionLevel.TIME_SENSITIVE]

    if not items:
        # A genuinely different, quieter state — not an empty dashboard shell.
        return ("empty", "Nothing needs you", "You're clear. Lifeline is watching the threads.")

    if len(urgent) >= SURGE_THRESHOLD:
        return (
            "surge",
            f"{len(urgent)} things are time-sensitive",
            "Everything else is collapsed until these are dealt with.",
        )

    if 6 <= hour < 11:
        lead = "Here's the morning" if not weekend else "Slow start"
        detail = (
            f"{len(urgent)} time-sensitive, {len([i for i in items if i.interruption_level == InterruptionLevel.ACTIVE])} to decide"
            if urgent
            else "Nothing urgent — good time for the decisions."
        )
        return ("briefing", lead, detail)

    if hour >= 20 or hour < 6:
        return ("evening", "Winding down", "Only what actually can't wait.")

    if weekend:
        return ("day", "The weekend list", None)

    return ("day", "Where things stand", None)


def _style_for(level: str, mode: str, count: int) -> str:
    """Level of detail adapts to urgency (§8.1)."""
    if level == InterruptionLevel.PASSIVE:
        return "collapsed"
    if level == InterruptionLevel.TIME_SENSITIVE:
        return "expanded"
    if mode == "surge":
        return "collapsed"
    if mode == "evening":
        return "compact"
    return "compact" if count > 6 else "expanded"


_TITLES = {
    InterruptionLevel.TIME_SENSITIVE: "Needs you now",
    InterruptionLevel.ACTIVE: "When you get to it",
    InterruptionLevel.PASSIVE: "No rush",
}


def build_today(items: List[Item], reference: Optional[datetime] = None) -> Dict[str, object]:
    now = reference or datetime.now(timezone.utc)
    mode, headline, subhead = _mode(items, now)
    buckets = _bucket(items)

    groups: List[GroupOut] = []
    for level in InterruptionLevel.ALL:
        bucket = buckets.get(level) or []
        if not bucket:
            continue

        # §6.4: in the morning, hold Passive items entirely — they're for
        # whenever there's idle time, not for the decision window.
        if mode == "briefing" and level == InterruptionLevel.PASSIVE:
            continue
        if mode == "surge" and level == InterruptionLevel.PASSIVE:
            continue
        if mode == "evening" and level == InterruptionLevel.ACTIVE and len(bucket) > 3:
            bucket = bucket[:3]

        style = _style_for(level, mode, len(bucket))
        subtitle = None
        if style == "collapsed":
            people = sorted({i.person for i in bucket})
            subtitle = f"{len(bucket)} from {', '.join(people[:3])}" + (
                f" +{len(people) - 3}" if len(people) > 3 else ""
            )
        groups.append(
            GroupOut(
                level=level,
                title=_TITLES.get(level, level),
                subtitle=subtitle,
                style=style,
                items=[ItemOut.of(i) for i in bucket],
            )
        )

    counts = {level: len(buckets.get(level) or []) for level in InterruptionLevel.ALL}
    counts["total"] = len(items)
    return {
        "mode": mode,
        "headline": headline,
        "subhead": subhead,
        "generated_at": now.isoformat(timespec="seconds"),
        "groups": groups,
        "counts": counts,
    }
