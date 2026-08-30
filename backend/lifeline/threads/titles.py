"""Thread headlines (§v2.9 follow-on, from `eval-titles-are-raw-sentences`).

A declared thread's title used to be the user's raw sentence — "Nia is
continuously asking me to find some sexy pajamas, or a matching set. So I
wanna get like 3 sets for between $100-150." ran four lines of serif in the
feed with a strikethrough through it. The sentence is the *declaration* and
it is kept, verbatim, as the summary; the feed slot gets a headline written
from the context the thread builds.

Asynchronous by design: declare stays instant (the arrival animation flies
the typed words), and the headline lands on the next refresh. The worker
retitles as a safety net for threads declared while no provider was up.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .. import db
from ..extraction import providers

log = logging.getLogger(__name__)

MAX_WORDS = 7

_SYSTEM = (
    "You write feed headlines for a personal task app. Given what the user "
    "typed when declaring an open loop, and any context, return JSON "
    '{"title": "..."} — a headline of at most 7 words. Noun-phrase or short '
    "imperative, no ending period, keep names and concrete objects, drop "
    "prices, dates and filler. Never invent details that are not in the text."
)

# A title that reads like a typed sentence rather than a headline: long, or
# carrying first-person narration. Generated and system titles never match.
_RAW = re.compile(r"\b(i|i'm|i'll|my|me|we|wanna|gotta|needa?)\b", re.I)


def looks_raw(title: str) -> bool:
    words = (title or "").split()
    return len(words) > MAX_WORDS or bool(_RAW.search(title or ""))


def headline_for(text: str, context: str = "") -> Optional[str]:
    """A ≤7-word headline, or None when no provider answers or the answer is
    unusable. Never raises."""
    prompt = f"The user typed:\n{text.strip()}\n"
    if context:
        prompt += f"\nContext the system has gathered:\n{context[:800]}\n"
    prompt += '\nReturn JSON only: {"title": "..."}'
    try:
        raw = providers.run(
            lambda p: p.complete_json(prompt, system=_SYSTEM, max_tokens=60),
            "thread title",
        )
        if not raw:
            return None
        # Models fence their JSON ("```json\n{...}\n```") — the same lesson
        # topic titles learned. Take the first object in the text.
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return None
        title = str(json.loads(match.group(0)).get("title") or "").strip().strip('"').rstrip(".")
        words = title.split()
        if not (1 <= len(words) <= MAX_WORDS + 2):
            return None
        return " ".join(words)
    except Exception as exc:
        log.warning("headline generation failed: %s", exc)
        return None


def retitle(thread_id: str) -> bool:
    """Give a user-declared thread a generated headline, keeping the typed
    sentence as the summary. Idempotent: a thread whose title no longer looks
    raw is left alone, so a user's manual rename is never overwritten."""
    thread = db.get_thread(thread_id)
    if thread is None or thread.origin != "user" or not looks_raw(thread.title):
        return False

    context_bits = []
    for e in db.thread_evidence(thread.id)[:4]:
        if e.kind == "item":
            item = db.get_item(e.ref_id)
            if item:
                context_bits.append(item.suggested_action or item.raw_text[:80])
    headline = headline_for(thread.title, "\n".join(context_bits))
    if not headline or headline == thread.title:
        return False

    from . import update  # local import keeps the layering acyclic

    update(
        thread_id,
        title=headline,
        # The typed sentence is the declaration; it survives as the summary
        # unless the user already wrote one.
        summary=thread.summary or thread.title,
    )
    log.info("retitled %r -> %r", thread.title[:40], headline)
    return True


def sweep(limit: int = 5) -> int:
    """The safety net, run by the poller: any live user-declared thread still
    wearing a raw sentence gets a headline. Capped per cycle."""
    retitled = 0
    for thread in db.list_threads():
        if retitled >= limit:
            break
        if thread.state in ("live", "quiet") and thread.origin == "user" \
                and looks_raw(thread.title):
            if retitle(thread.id):
                retitled += 1
    return retitled
