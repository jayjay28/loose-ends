"""Understand before you surface. Instead of a template title, the context
engine investigates an item — how often a message like this has come, who it's
really from, the surrounding conversation, whether you replied before — and turns
those findings into a grounded headline + briefing. LLM when available; a
findings-based assembly otherwise (still far better than a template).
"""
from __future__ import annotations

import json
from typing import Dict, List

from .. import db
from ..extraction import providers
from ..extraction.claude import _parse_json
from ..extraction.heuristic import _clean_subject
from ..models import Item
from . import tools

ENRICH_SYSTEM = (
    "You turn a raw action item into a briefing the user can act on in seconds. "
    "You get the item plus what the context engine found: how often a message "
    "like this has arrived, who it's from and how close they are, the surrounding "
    "conversation, and whether the user replied to earlier ones. Produce:\n"
    "- headline: a specific, plain-language title capturing the real situation "
    "(who + what + any pattern) — never a template, <= 9 words.\n"
    "- briefing: one or two sentences on what this is and what the findings mean "
    "(e.g. 'third notice like this, ~one every two weeks; you replied last time'). "
    "Ground every claim in the findings; invent nothing.\n"
    'Return JSON only: {"headline": "...", "briefing": "..."}.'
)


def _findings(item: Item) -> dict:
    stats = tools.similar_message_stats(item)
    person = tools.find_person(item.person) if item.person else None
    ctx = db.message_context(item.conversation_id, item.timestamp, before=3, after=1) if item.conversation_id else []
    return {
        "item": {"type": item.type, "raw_text": (item.raw_text or "")[:400],
                 "action": item.suggested_action, "person": item.person},
        "frequency": stats,
        "person": ({"tie": person["tie"], "tie_strength": person["tie_strength"]} if person else None),
        "conversation": [
            {"who": "You" if m.is_from_user else item.person, "text": (m.text or "")[:160]}
            for m in ctx
        ],
    }


def enrich_item(item: Item) -> dict:
    findings = _findings(item)
    prompt = "Findings:\n" + json.dumps(findings, indent=2) + "\n\nWrite the headline and briefing."
    text = providers.run(
        lambda p: p.complete_json(prompt, system=ENRICH_SYSTEM, max_tokens=220),
        "item enrichment",
    )
    if text:
        try:
            data = _parse_json(text)
            if data.get("headline"):
                return {
                    "headline": data["headline"].strip(),
                    "briefing": (data.get("briefing") or "").strip(),
                    "sources": _sources(findings),
                }
        except Exception:
            pass
    return _fallback(item, findings)


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _fallback(item: Item, findings: dict) -> dict:
    freq = findings["frequency"]
    message = db.get_message(item.message_id) if item.message_id else None
    subject = ""
    if item.source == "gmail" and message:
        subject = _clean_subject(message.metadata.get("subject") or "")

    headline = subject or item.suggested_action or (item.raw_text or "")[:50]
    if freq.get("recurring") and freq.get("count"):
        headline = f"{_ordinal(freq['count'])} · {headline}"

    bits: List[str] = []
    if freq.get("recurring"):
        s = f"You've had {freq['count']} messages like this"
        if freq.get("cadence_days"):
            s += f" — about one every {freq['cadence_days']} days"
        bits.append(s + ".")
        if freq.get("you_replied_before"):
            bits.append(f"You replied to {freq['you_replied_before']} of the earlier ones.")
    if findings["person"]:
        bits.append(f"From {findings['item']['person']} — {findings['person']['tie']}.")
    briefing = " ".join(bits) or (item.suggested_action or "")

    return {"headline": headline, "briefing": briefing, "sources": _sources(findings)}


def _sources(findings: dict) -> List[str]:
    out: List[str] = []
    freq = findings["frequency"]
    if freq.get("count"):
        out.append(f"{freq['count']} similar message(s)")
    if findings["person"]:
        out.append(f"relationship: {findings['person']['tie']}")
    if findings["conversation"]:
        out.append(f"{len(findings['conversation'])} messages of context")
    return out
