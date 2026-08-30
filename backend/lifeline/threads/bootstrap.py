"""First-run migration: turn the user's *open* items into threads.

The spec is explicit about the scope, and the audit is explicit about why: the
live database holds ~170 items, but only about twenty are open. Clustering the
rest would hand the user their finished work back as new loops to carry — the
precise opposite of the product. So this touches open items only; the closed
ones stay exactly what they already are, which is evidence.

Two clusterers live here so the question "does an LLM pass earn itself at this
size?" could be answered with a run rather than an opinion:

- `cluster_deterministic` — group by person, then split a person's items on
  shared distinctive terms. No model, no cost, no latency.
- `cluster_llm` — one completion over the whole open set.

**What the comparison showed, on the live database's 18 open items:** the
deterministic pass returns *16* threads. It merges only Jazbo's two and
Mummy's two, which means it is within rounding distance of one-thread-per-item
— and its titles are the sender's own subject lines ("Your Upcoming Stay at San
Juan Marriott Resort & Stellaris Casino", "Hi Alex, here's what changed").
Both halves of the value are missing: it does not reduce the pile, and the
names on the pile are marketing copy rather than the loop the user is carrying.
That is what the LLM pass is for, and one ~2k-token call is not a cost worth
avoiding for a once-ever migration. So the LLM pass is the default, and the
deterministic one is the fallback for offline runs and for when the pass comes
back untrustworthy — which is checked, not assumed: a clusterer that drops any
of the user's open work is rejected outright.

`compare()` runs both and prints them side by side.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from .. import db
from ..models import (
    DeadlineSource,
    Evidence,
    Item,
    Thread,
    ThreadOrigin,
    ThreadState,
)
from . import ThreadError, create, set_deadline

log = logging.getLogger(__name__)

_STOP = {
    "the", "and", "you", "your", "for", "with", "this", "that", "have", "from",
    "about", "will", "would", "there", "their", "them", "just", "please", "when",
    "what", "were", "been", "http", "https", "com", "www", "sent", "reply",
    "message", "email", "here", "get", "got", "can", "back", "know",
    "need", "want", "make", "let", "see", "day", "days", "time", "more", "info",
    # Generic action verbs. These come from `suggested_action`, which every
    # item has, so leaving them in would say two items are about the same
    # thing because both are things to do.
    "handle", "check", "respond", "follow", "followup", "ask", "tell", "look",
}
_WORD = re.compile(r"[a-z0-9']{3,}")

# A term carried by at most this many items is specific enough that sharing it
# means two items are about the same thing.
RARE_AT = 2


# ------------------------------------------------------------ clustering
def _terms(item: Item) -> set:
    """The distinctive words that say what an item is *about*."""
    text = " ".join(
        filter(None, [item.entities.item, item.suggested_action, item.raw_text])
    ).lower()
    return {w for w in _WORD.findall(text) if w not in _STOP and not w.isdigit()}


def cluster_deterministic(items: Sequence[Item]) -> List[Dict[str, Any]]:
    """Person, then topic. Items from the same person join the same cluster
    when they share a *distinctive* word; otherwise the person's items split.

    That second half matters: five open items with one person are rarely one
    loop. "Kids' investment account" and "concert tickets" are not the same
    thing just because the same person sent both.

    Distinctive means rare across the whole open set: at most `RARE_AT` items
    carry it. Measured on the real database, a single shared common word merged
    three unrelated items from one person — all three said "out" ("check out",
    "you're out") — and "kids" pulled a skating trip into an investment
    account. One common word is not a topic, so a merge needs either two shared
    words or one rare one.
    """
    df: Dict[str, int] = {}
    for item in items:
        for term in _terms(item):
            df[term] = df.get(term, 0) + 1

    by_person: Dict[str, List[Item]] = {}
    for item in items:
        by_person.setdefault(item.person_id or f"name:{item.person}", []).append(item)

    clusters: List[Dict[str, Any]] = []
    for group in by_person.values():
        for bucket in _split_by_topic(group, df):
            clusters.append(
                {
                    "title": _title_for(bucket),
                    "summary": _summary_for(bucket),
                    "item_ids": [i.id for i in bucket],
                }
            )
    clusters.sort(key=lambda c: -len(c["item_ids"]))
    return clusters


def _split_by_topic(group: List[Item], df: Dict[str, int]) -> List[List[Item]]:
    """Greedy merge over shared distinctive terms, within one person."""
    buckets: List[List[Item]] = []
    bucket_terms: List[set] = []
    for item in sorted(group, key=lambda i: i.timestamp):
        terms = _terms(item)
        joined = False
        for n, existing in enumerate(bucket_terms):
            shared = terms & existing
            if len(shared) >= 2 or any(df.get(t, 99) <= RARE_AT for t in shared):
                buckets[n].append(item)
                bucket_terms[n] = existing | terms
                joined = True
                break
        if not joined:
            buckets.append([item])
            bucket_terms.append(terms)
    return buckets


def _title_for(bucket: List[Item]) -> str:
    """A thread's title is the whole surface the user reads, so prefer the
    extracted entity over the raw sentence, and never let it run long."""
    lead = max(bucket, key=lambda i: (bool(i.entities.item), i.score))
    for candidate in (lead.entities.item, lead.suggested_action, lead.raw_text):
        text = " ".join((candidate or "").split())
        if text:
            title = text[:70].rstrip()
            if len(bucket) > 1:
                title = f"{title} ({lead.person})"
            return title
    return f"Open loop with {lead.person}"


def _summary_for(bucket: List[Item]) -> str:
    lines = [
        f"- {(i.entities.item or i.suggested_action or i.raw_text or '')[:100]}" for i in bucket
    ]
    return "\n".join(lines)[:600]


CLUSTER_SYSTEM = (
    "You group a person's open loose ends into the smallest set of coherent "
    "threads. A thread is one open loop in their head — a trip, a bill, a "
    "decision, a conversation they owe. Merge only what genuinely belongs to "
    "the same loop; two items from the same person are usually two loops. "
    "Title each thread as the loop the user is carrying, in their own terms "
    "and under 60 characters — not the sender's subject line. Every item id "
    "must appear exactly once."
)

# Structured outputs: the shape is known, so constrain it rather than hoping.
CLUSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "threads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "summary", "item_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["threads"],
    "additionalProperties": False,
}


def cluster_llm(items: Sequence[Item]) -> Optional[List[Dict[str, Any]]]:
    """One completion over the whole open set. Returns None when no provider is
    configured or the response can't be trusted — the caller then uses the
    deterministic pass, which is the default anyway."""
    from ..extraction import providers
    from ..extraction.claude import _parse_json

    if not items:
        return []
    lines = [
        f"{i.id} | {i.person} | {i.type} | {(i.entities.item or i.suggested_action or i.raw_text or '')[:120]}"
        + (f" | due {i.entities.date[:10]}" if i.entities.date else "")
        for i in items
    ]
    prompt = "The user's open items:\n" + "\n".join(lines)
    text = providers.run(
        lambda p: p.complete_json(
            prompt, system=CLUSTER_SYSTEM, max_tokens=2000, schema=CLUSTER_SCHEMA
        ),
        "thread clustering",
    )
    if not text:
        return None
    try:
        clusters = _parse_json(text).get("threads") or []
    except Exception as exc:
        log.warning("thread clustering returned unparseable JSON: %s", exc)
        return None

    known = {i.id for i in items}
    cleaned: List[Dict[str, Any]] = []
    seen: set = set()
    for c in clusters:
        ids = [i for i in (c.get("item_ids") or []) if i in known and i not in seen]
        if not ids or not (c.get("title") or "").strip():
            continue
        seen.update(ids)
        cleaned.append(
            {"title": c["title"].strip()[:80], "summary": (c.get("summary") or "").strip(), "item_ids": ids}
        )
    # A clusterer that drops the user's open work is worse than no clusterer.
    missing = [i for i in items if i.id not in seen]
    if missing:
        log.warning("thread clustering dropped %d items; falling back", len(missing))
        return None
    return cleaned


# --------------------------------------------------------------- running
def open_items_to_thread() -> List[Item]:
    """The migration surface: open items not already claimed by a thread."""
    return [i for i in db.open_items() if not db.threads_claiming("item", i.id)]


def run(use_llm: bool = True, dry_run: bool = False) -> Dict[str, Any]:
    """Bootstrap threads from the open items.

    Idempotent: an item already claimed by a thread is skipped, so running this
    twice does not double the user's pile. Falls back to the deterministic
    clusterer whenever the LLM pass is unavailable or untrustworthy, so this
    always produces something offline.
    """
    items = open_items_to_thread()
    if not items:
        return {"items": 0, "threads": 0, "clusterer": None, "created": []}

    clusters = cluster_llm(items) if use_llm else None
    clusterer = "llm"
    if clusters is None:
        clusters, clusterer = cluster_deterministic(items), "deterministic"
    if dry_run:
        return {"items": len(items), "threads": len(clusters), "clusterer": clusterer,
                "created": clusters}

    by_id = {i.id: i for i in items}
    created: List[Dict[str, Any]] = []
    for cluster in clusters:
        members = [by_id[i] for i in cluster["item_ids"] if i in by_id]
        if not members:
            continue
        # These are loops the user is *already* carrying — they were open in
        # the deck. Landing them as proposals would hide the user's own work
        # behind an opt-in tab, so they open live.
        thread = create(
            title=cluster["title"],
            summary=cluster.get("summary") or _summary_for(members),
            origin=ThreadOrigin.PROMOTED,
            state=ThreadState.LIVE,
            evidence=[
                Evidence(kind="item", ref_id=m.id, role="founding" if n == 0 else "claimed")
                for n, m in enumerate(members)
            ],
        )
        _carry_deadline(thread, members)
        created.append({"thread_id": thread.id, "title": thread.title, "items": len(members)})

    log.info("bootstrap: %d open items → %d threads (%s)", len(items), len(created), clusterer)
    return {"items": len(items), "threads": len(created), "clusterer": clusterer, "created": created}


def _carry_deadline(thread: Thread, members: List[Item], reference=None) -> None:
    """The most *relevant* date any member carries becomes the thread's
    deadline, with that item as the receipt.

    Not simply the soonest. These are pre-existing items, so many of their
    dates are already in the past — and taking the minimum guarantees picking
    the most stale one available. On the live database that produced a thread
    dated three months back. So: the soonest date still ahead of us, and only
    if there is none, the most recently passed one (which is real information —
    the loop had a date and it is gone — just not urgency).
    """
    from datetime import datetime, timezone

    from ..models import parse_iso

    now = reference or datetime.now(timezone.utc)
    dated = sorted(
        (m for m in members if parse_iso(m.entities.date)),
        key=lambda m: parse_iso(m.entities.date),
    )
    if not dated:
        return
    upcoming = [m for m in dated if parse_iso(m.entities.date) >= now]
    lead = upcoming[0] if upcoming else dated[-1]
    try:
        set_deadline(
            thread.id,
            lead.entities.date,
            source=DeadlineSource.INFERRED,
            evidence=[{"kind": "item", "ref_id": lead.id}],
            reason=f"{lead.person}: {(lead.entities.item or lead.raw_text or '')[:100]}",
        )
    except ThreadError as exc:
        log.warning("could not carry deadline onto %s: %s", thread.id, exc)


def compare() -> Dict[str, Any]:
    """Run both clusterers over the same open items and hand back both answers.
    The point is to be able to *look*, not to be told which is better."""
    items = open_items_to_thread()
    llm = cluster_llm(items)
    return {
        "items": len(items),
        "deterministic": cluster_deterministic(items),
        "llm": llm,
        "llm_available": llm is not None,
    }


def render(clusters: List[Dict[str, Any]], items: Sequence[Item]) -> str:
    by_id = {i.id: i for i in items}
    out: List[str] = []
    for c in clusters:
        out.append(f"  {c['title']}")
        for iid in c["item_ids"]:
            item = by_id.get(iid)
            if item:
                label = (item.entities.item or item.suggested_action or item.raw_text or "")[:60]
                out.append(f"      · {item.person[:22]:<22} {label}")
    return "\n".join(out)
