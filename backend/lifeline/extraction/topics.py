"""Short topic labels for threads — "what this is about" for the Threads list.

One grouped LLM call labels every thread at once (rate-friendly), with a
deterministic heuristic fallback so it always returns something, even offline.
The API layer caches the result per thread so this only runs when a thread's
open items actually change.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List

from ..config import get_config
from . import providers

log = logging.getLogger(__name__)

_SYSTEM = (
    "You write a one-line title for a conversation thread — what it's about and "
    "where it stands. You're given the other person and the open to-dos from "
    "that conversation (things the user still owes them). Write a specific, "
    "glanceable line of at most 8 words that names the subject and, when it "
    "matters, the state: who's waiting, what's unanswered, what's unfinished. "
    "Prefer \"Family lawyer visit — Kay's waiting to hear back\" over \"Family "
    "lawyer\". Never a bare noun. Do NOT include dates, ages, or how long it has "
    "been — that is shown separately. Respond ONLY as JSON: "
    '{"topics": {"<key>": "<title>"}}.'
)

_STOP = {"prepare", "for", "the", "a", "to", "with", "about", "reply", "send", "call", "back", "your", "you"}


def _shorten(text: str, n: int = 4) -> str:
    words = [w for w in re.sub(r"[^\w\s]", "", text).split() if w.lower() not in _STOP]
    return " ".join(words[:n]).strip().capitalize()


def heuristic_title(person: str, snippets: List[str]) -> str:
    """Instant, network-free title for one thread (request-path safe)."""
    return _heuristic([{"key": "_", "person": person, "items": snippets}])["_"]


def refresh_thread_topics() -> int:
    """Poll-time: (re)generate cached thread titles with ONE grouped LLM call.
    Only threads whose open-item set changed are touched; cached in sync_state
    for the /threads request path to read instantly. Budget-gated.
    """
    from .. import db
    from . import budget

    open_items = [i for i in db.list_items() if i.status in ("pending", "snoozed")]
    groups: Dict[str, Dict] = {}
    for it in open_items:
        key = it.person_id or it.person
        g = groups.setdefault(key, {"key": key, "person": it.person, "ids": [], "items": []})
        g["ids"].append(it.id)
        g["items"].append(it.suggested_action or it.raw_text)

    pending = []
    for g in groups.values():
        sig = str(sorted(g["ids"]))
        cached = db.get_sync_state(f"topic:v2:{g['key']}")
        if cached and cached.split("\n", 1)[0] == sig:
            continue
        g["sig"] = sig
        pending.append(g)
    if not pending:
        return 0

    cfg = get_config()
    has_llm = cfg.has_claude or cfg.has_gemini
    if has_llm and budget.allow():
        budget.record()
        titles = classify([{"key": g["key"], "person": g["person"], "items": g["items"]} for g in pending])
    elif not has_llm:
        # No provider at all — cache the heuristic so titles stay stable.
        titles = {g["key"]: heuristic_title(g["person"], g["items"]) for g in pending}
    else:
        # Provider exists but budget is spent — leave uncached so a later
        # cycle can still upgrade to a real LLM title.
        return 0

    written = 0
    for g in pending:
        if title := titles.get(g["key"]):
            db.set_sync_state(f"topic:v2:{g['key']}", f"{g['sig']}\n{title}")
            written += 1
    return written


def _heuristic(threads: List[Dict]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for t in threads:
        items = t.get("items") or []
        title = _shorten(items[0]) if items else ""
        out[t["key"]] = title or f"With {t.get('person', 'someone')}"
    return out


def classify(threads: List[Dict]) -> Dict[str, str]:
    """threads: [{key, person, items:[snippet, ...]}] -> {key: title}."""
    if not threads:
        return {}
    base = _heuristic(threads)

    prompt = "Threads:\n" + json.dumps(
        [{"key": t["key"], "person": t.get("person"), "items": (t.get("items") or [])[:6]} for t in threads]
    )
    from .claude import _parse_json

    text = providers.run(
        lambda p: p.complete_json(prompt, system=_SYSTEM, max_tokens=512), "topic titles"
    )
    if text:
        try:
            topics = _parse_json(text).get("topics", {})
            if isinstance(topics, dict):
                # LLM titles win; heuristic fills any the model skipped.
                return {**base, **{k: v for k, v in topics.items() if isinstance(v, str) and v.strip()}}
        except Exception as exc:  # malformed output
            log.warning("topic titles unparseable, using heuristic: %s", exc)
    return base
