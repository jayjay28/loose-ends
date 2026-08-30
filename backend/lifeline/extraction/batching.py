"""Phase D — fold several open items owed to one person into a single reply.

"You don't owe them tasks, you owe them a message." The user stages a few open
loops with one person; this composes the one reply that answers them together.
LLM when a provider is configured and within budget, a plain stitch otherwise —
same graceful-degradation contract as the rest of extraction (§5, §9).
"""
from __future__ import annotations

import logging
from typing import List

from ..models import Item
from . import providers

log = logging.getLogger(__name__)


def _spec(item: Item) -> dict:
    return {
        "type": item.type,
        "raw_text": item.raw_text,
        "suggested_action": item.suggested_action,
        "suggested_reply": item.suggested_reply,
        "entity_item": item.entities.item,
    }


def compose_reply(person_name: str, items: List[Item]) -> str:
    """One outgoing message that answers all of `items`."""
    if not items:
        return ""
    reply = providers.run(
        lambda p: p.compose_batch_reply(person_name, [_spec(i) for i in items]),
        "batch reply compose",
    )
    if reply and reply.strip():
        return reply.strip()
    return _stitch(items)


def _stitch(items: List[Item]) -> str:
    """Heuristic fallback: the user's own drafted replies, joined plainly. Only
    real reply text goes into an outgoing message — never the imperative
    "what you owe" action lines, which are notes to self, not to the recipient."""
    parts: List[str] = []
    for item in items:
        piece = (item.suggested_reply or "").strip().rstrip(".")
        if piece and piece not in parts:
            parts.append(piece)
    return ". ".join(parts) + ("." if parts else "")
