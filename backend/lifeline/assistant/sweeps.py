"""Idle sweeps (§v1.4 pillar A) — standing investigations the poller runs each
cycle. The first: silence detection. You spoke last and nobody answered; the
system notices the open loop you may have forgotten.

**§v2 step 1: the sweep now opens a thread, not an item.** "X went quiet" *is*
an open loop the user is carrying — it is the clearest example in the codebase
of the thing threads are for. Under the new routing rule an `information` item
has no thread, so it would have become a proposal, and a push today would be an
opt-in tab tomorrow. That is a regression in the one place v2 claims to be
strongest, so the sweep produces a **live** thread directly, with the silence
as its founding evidence.

Deterministic candidate-finding (cheap, no LLM); the worker loop deepens what
these create in step 4.

**One thing this trade costs, stated rather than inherited quietly:** as items
these could ride the morning briefing and the passive digest
(`notifications/scheduler.py` is item-keyed throughout). As threads they reach
the user only in the app until notifications become thread-scoped in step 7c.
Nothing is lost on the live database today — the candidate funnel there yields
zero, every silence being with an unnamed number — but the gap is real and it
closes in 7c, not by accident.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from .. import db
from .. import threads as threads_mod
from ..models import Evidence, ThreadOrigin, ThreadState, parse_iso
from ..ranking import relationships

log = logging.getLogger(__name__)

# The shapes Messages gives a reaction when it arrives as text.
_TAPBACK = __import__("re").compile(
    r'^(Liked|Loved|Laughed at|Emphasized|Disliked|Questioned)\s+[“"]')

SILENCE_DAYS = 4          # you've waited this long with no answer
MAX_SILENCE_DAYS = 45     # older than this is presumed abandoned, not surfaced
MIN_TIE = 0.08            # only people who matter — no receipts bots


def source_horizon(source: str) -> Optional[datetime]:
    """The newest message this source has *delivered* — the last moment it can
    honestly say anything about.

    Inbound only. An outbound row proves the user typed something; an inbound
    one proves the pipe is still open, which is the thing being asked about.
    """
    row = db.get_connection().execute(
        "SELECT MAX(timestamp) FROM messages WHERE source = ? AND is_from_user = 0",
        (source,),
    ).fetchone()
    return parse_iso(row[0]) if row and row[0] else None


def source_freshness() -> Dict[str, str]:
    """Every source and when it last delivered, for anything that reasons about
    silence — the sweep below, and the worker's brief."""
    rows = db.get_connection().execute(
        "SELECT source, MAX(timestamp) FROM messages WHERE is_from_user = 0 GROUP BY source"
    ).fetchall()
    return {r[0]: r[1] for r in rows if r[1]}


def silence_sweep(reference: Optional[datetime] = None) -> int:
    """One live thread per 1:1 conversation where the user's message is the
    last word and the silence has run long enough to be worth carrying."""
    now = reference or datetime.now(timezone.utc)
    conn = db.get_connection()
    ties = relationships.strengths(now, force=True)  # fresh — the poller runs this once a cycle

    rows = conn.execute(
        """
        SELECT m.id, m.conversation_id, m.person_id, m.timestamp, m.text, t.display_name, t.source
        FROM messages m
        JOIN conversations t ON t.id = m.conversation_id
        WHERE t.is_group = 0
          AND m.timestamp = (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.conversation_id = m.conversation_id)
          AND m.is_from_user = 1
        """
    ).fetchall()

    created = 0
    horizons: Dict[str, Optional[datetime]] = {}
    for row in rows:
        ts = parse_iso(row["timestamp"])
        if ts is None:
            continue

        # Silence is measured against what the source has actually witnessed.
        #
        # This used to be `(now - ts).days`, which asks the clock a question
        # only the store can answer. When iMessage stopped ingesting on
        # 2026-08-22 every conversation in it went quiet four days later on
        # schedule: three of the four live silence threads were opened against
        # a frozen store, about people who may well have replied. "X went
        # quiet" is the clearest open loop this product knows how to name, and
        # naming it wrong — about a specific person, by name — is worse than
        # not naming it at all.
        source = row["source"]
        if source not in horizons:
            horizons[source] = source_horizon(source)
        horizon = horizons[source]
        if horizon is None or (now - horizon).days >= SILENCE_DAYS:
            continue      # the source went quiet, not the person

        days = (horizon - ts).days
        if not (SILENCE_DAYS <= days <= MAX_SILENCE_DAYS):
            continue

        person_id = row["person_id"]
        person = db.get_person(person_id) if person_id else None
        if not person or not any(ch.isalpha() for ch in person.display_name):
            continue  # unknown number — the resolver agent's job, not a nudge
        if relationships._is_service(person_id):
            continue
        if ties.get(person_id, 0.0) < MIN_TIE:
            continue

        # A tapback is an acknowledgment, not a question awaiting an answer.
        # "Liked “OK, we'll talk later”" as the last word founded a live thread
        # claiming Theo B went quiet — while his actual reply sat one row up.
        # A reaction closes an exchange; nobody owes anybody after it.
        if _TAPBACK.match((row["text"] or "").strip()):
            continue

        key = silence_key(row["conversation_id"], row["timestamp"])
        if _already_carried(row["conversation_id"], key):
            continue

        last_word = (row["text"] or "").strip()
        threads_mod.create(
            # The thread is *about* this person; without the contact the
            # write-time draft path refuses ("no person_id") and the app's
            # Review & send button dies silently — found live, 2026-08-29.
            contact_person_id=person.id,
            title=f"{person.display_name} went quiet",
            summary=(
                f"You messaged {person.display_name} {days} days ago and haven't heard back. "
                f"Your last word: “{last_word[:120]}”"
            ),
            origin=ThreadOrigin.SILENCE,
            state=ThreadState.LIVE,
            key=key,
            evidence=[
                Evidence(
                    kind="message",
                    ref_id=row["id"],
                    role="founding",
                    note=f"your last word, {days} days ago",
                )
            ],
        )
        created += 1
    if created:
        log.info("silence sweep: opened %d quiet-loop threads", created)
    return created


def silence_key(conversation_id: str, last_user_timestamp: str) -> str:
    """The dedupe key. Keyed on *which silence* — the conversation plus the
    message that started it — so a sweep running every five minutes can't pile
    up duplicates, while a new message from the user genuinely starts a new
    silence and is allowed to open a new thread later."""
    return f"silence:{conversation_id}:{last_user_timestamp}"


def _already_carried(conversation_id: str, key: str) -> bool:
    """One nudge per silence, preserving the v1.4 rule exactly: skip while a
    silence thread for this conversation is still open, and skip a silence the
    user has already dealt with — but let a *new* silence through."""
    for thread in db.list_threads(key_prefix=f"silence:{conversation_id}:"):
        if thread.state in ThreadState.OPEN:
            return True
        if thread.key == key:      # this exact silence, already resolved/archived
            return True
    return False


# How many loops the system may offer in one cycle. A proposals list is a
# thing a person reads; an inbox of two hundred is a thing they abandon.
PROPOSE_PER_CYCLE = 5
# Below this, an extracted item is a guess. Proposing guesses teaches people
# to ignore the proposals, which costs more than the misses.
PROPOSE_FLOOR = 0.55


def propose_sweep(limit: int = PROPOSE_PER_CYCLE) -> int:
    """Offer the best unattached items as loops the user might be carrying.

    Extraction was filling the items table and stopping there: 541 open items
    on the author's own engine against one new thread in three hours, because
    nothing connected the two. The app shows threads, so an item no thread
    claims is a loose end the product found and never mentioned.

    They arrive as *proposals*, not live threads. The spec is explicit that
    only the user's acceptance puts a loop on the pile — that is what keeps
    the count meaningful — so this fills the proposals drawer and nothing
    else.
    """
    from .. import db
    from ..threads import promote_item
    from ..models import ThreadState

    rows = db.get_connection().execute(
        """
        SELECT i.id FROM items i
        WHERE i.status = 'pending'
          AND i.score >= ?
          AND i.id NOT IN (SELECT ref_id FROM thread_evidence WHERE kind = 'item')
        ORDER BY i.score DESC, i.created_at DESC
        LIMIT ?
        """,
        (PROPOSE_FLOOR, limit),
    ).fetchall()

    proposed = 0
    for row in rows:
        try:
            promote_item(row["id"], state=ThreadState.PROPOSED)
            proposed += 1
        except Exception:
            log.exception("propose failed for item %s", row["id"])
    if proposed:
        log.info("proposed %d loop(s) from unattached items", proposed)
    return proposed


def run_all(reference: Optional[datetime] = None) -> int:
    """Every standing sweep, called by the poller each cycle."""
    return silence_sweep(reference) + propose_sweep()
