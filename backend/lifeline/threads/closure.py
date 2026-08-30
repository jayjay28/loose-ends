"""Evidence-based closure for threads (§v2 step 5).

The mechanism that makes the pile shrink. Everything before this step only adds
to it, which is why the audit pulled closure inside the cut line: v1.5 closes
147 of 172 items, about half without the user, and shipping v2 without that
would ship a system that only accumulates.

**This follows the completion engine's shape, not its code.** `completion/
matcher.py` scores entity overlap — `entity_item`, `entity_date`, money, order
references — and a thread has none of those fields. What carries over is the
architecture: evidence → confidence → auto-close / ask / ignore, with *named*
reasons, because closing something on the user's behalf has to be defensible.

Two things learned from the live database shape the thresholds:

1. **A thread's strongest closure signal is its own evidence closing.** Threads
   claim items, and v1.5 already closes items well (76 of them auto-closed on
   an iMessage self-reply). When every item a thread claims is done, the
   thread's work is done. That is definite, cheap, and reuses machinery that
   has already earned its confidence.

2. **The ask band has to be stricter here than it is for items.** In this
   database v1.5's `needs_confirmation` produced 112 questions: 2 confirmed,
   37 explicitly rejected, the rest never answered. On the ones the user
   actually answered it was wrong 95% of the time. A product whose promise is
   *fewer* threads cannot afford to replace a pile of threads with a pile of
   questions about threads — so a thread is only ever *asked* about on
   structural evidence (its items closing, its date passing), never on a
   phrase that merely sounds like a receipt.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .. import db
from ..completion import matcher
from ..extraction.dates import days_until
from ..models import ThreadState, parse_iso

log = logging.getLogger(__name__)

# The bands carry over from §7 unchanged — they are the user's existing
# expectation of what "closed on its own" versus "asked me" feels like.
AUTO_CLOSE = matcher.AUTO_CLOSE                 # 0.80
NEEDS_CONFIRMATION = matcher.NEEDS_CONFIRMATION  # 0.45

# Past this, a date having gone by stops meaning "overdue" and starts meaning
# "over". Below it, a missed date is the opposite of closure.
STALE_DATE_DAYS = 14


@dataclass
class ThreadMatch:
    """Why a thread might be finished, in named parts."""

    thread_id: str = ""
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    evidence: List[Dict[str, str]] = field(default_factory=list)
    # Set when every part of the score came from something structural — items
    # actually closing, a date actually passing. A fuzzy resemblance never
    # auto-closes, no matter how confident it looks.
    definite: bool = False
    # The *shape* of the argument: which parts made it, not which rows they
    # cited. See `argument_key`.
    parts: List[str] = field(default_factory=list)

    @property
    def argument_key(self) -> str:
        """What makes this the same case as one already put to the user.

        Keyed on the parts rather than the evidence because the evidence moves.
        "Its date passed and you replied" is one argument whether the reply was
        this morning's or last night's, and a user who has already said no to
        it should not be asked again the next time they text that person.
        """
        return "+".join(sorted(set(self.parts)))

    @property
    def resolution(self) -> Optional[str]:
        if self.confidence >= AUTO_CLOSE and self.definite:
            return "auto_closed"
        if self.confidence >= NEEDS_CONFIRMATION:
            return "needs_confirmation"
        return None


def match(thread, reference: Optional[datetime] = None) -> ThreadMatch:
    """Score the case that this thread is finished."""
    now = reference or datetime.now(timezone.utc)
    result = ThreadMatch(thread_id=thread.id)

    confidence = 0.0
    definite_parts = 0
    fuzzy_parts = 0

    # --- 1. the thread's own evidence closing -------------------------------
    claimed = [e for e in db.thread_evidence(thread.id) if e.kind == "item"]
    items = [db.get_item(e.ref_id) for e in claimed]
    items = [i for i in items if i]
    if items:
        closed = [i for i in items if i.status in ("completed", "dismissed")]
        share = len(closed) / len(items)
        if share == 1.0:
            # Enough on its own. This is the definitive case: every concrete
            # thing the thread was tracking has been settled, each with its own
            # named reason from the completion engine.
            confidence += 0.85
            definite_parts += 1
            result.parts.append("items_all_closed")
            what = closed[0].suggested_action or closed[0].raw_text[:50]
            result.reasons.append(
                f"everything this thread was tracking is done"
                + (f" — {what}" if len(closed) == 1 else f" ({len(closed)} items)")
            )
            result.evidence += [{"kind": "item", "ref_id": i.id} for i in closed]
            # How the items closed is itself part of the argument.
            for item in closed[:3]:
                signals = [
                    s for s in db.signals_for_item(item.id)
                    if s.resolution in ("auto_closed", "confirmed")
                ]
                if signals and signals[0].reasons:
                    result.reasons.append(signals[0].reasons[0])
        elif share > 0:
            confidence += 0.25 * share
            definite_parts += 1
            result.parts.append("items_partly_closed")
            result.reasons.append(
                f"{len(closed)} of {len(items)} things this thread was tracking are done"
            )

    # --- 1b. a watcher went looking and found it ---------------------------
    # The strongest signal on the thread, and it was not being read at all.
    #
    # A watcher is a deterministic query the *worker* composed for this thread
    # — "mail from amwater confirming payment" — so when it fires the system
    # did not infer anything from vocabulary overlap: it asked a precise
    # question and got a yes. On the American Water thread three payment
    # confirmations were attached this way and the scan still declined,
    # because nothing here looked at them.
    #
    # Worth more than receipt language (§4) and less than every claimed item
    # closing (§1): on its own it asks, and alongside settled items it closes.
    confirmed = _watcher_confirmed(thread)
    if confirmed:
        confidence += 0.5
        definite_parts += 1
        result.parts.append("watcher_confirmed")
        result.reasons.append(confirmed["reason"])
        result.evidence += confirmed["evidence"]

    # --- 2. a date that has gone by ----------------------------------------
    # Weighted by *how long* gone. A bill whose date passed on Tuesday is
    # overdue, not done — the loop is more open than ever. A skating trip whose
    # date passed three months ago is simply over. The live database has
    # exactly that thread, dated May 16 and still sitting on the stack, which
    # is what this exists to clear.
    days = days_until(thread.deadline, now)
    if days is not None and days < -1:
        long_gone = abs(days) > STALE_DATE_DAYS
        confidence += 0.5 if long_gone else 0.3
        result.parts.append("date_passed")
        # A passed date is only *definite* when nothing concrete is still
        # outstanding. A skating trip whose date went by in May is over; a bill
        # whose date went by in May is the most open loop in the database, and
        # the difference between them is whether the thread's own items are
        # settled. Treated as structural either way, this combined with a
        # single reply to that person — 0.5 + 0.35 — auto-resolved overdue
        # threads on a "happy birthday".
        result.reasons.append(
            f"its date ({(thread.deadline or '')[:10]}) passed {abs(days):.0f} days ago"
        )
        if not items or _all_items_closed(items):
            definite_parts += 1
        else:
            fuzzy_parts += 1
            open_count = len([i for i in items if i.status not in ("completed", "dismissed")])
            result.reasons.append(
                f"but {open_count} of the things it was tracking "
                f"{'is' if open_count == 1 else 'are'} still open"
            )
    elif days is not None and days > 1 and _all_items_closed(items) and not confirmed:
        # Everything concrete is settled but the date is still ahead — the loop
        # is not over, whatever its evidence says. A trip whose hotel email is
        # filed has not happened yet.
        #
        # Unless a watcher confirmed it. The rule conflates two kinds of date:
        # one you must act *before* (a bill due the 31st) and one on which
        # something *happens* (a concert on the 29th). Acting early is the
        # whole point of the first kind, so clamping there punished the user
        # for paying on time — the American Water thread scored 0.85 on its
        # own evidence, was clamped to 0.40 for being nine days early, and sat
        # open with three payment confirmations attached to it.
        confidence = min(confidence, NEEDS_CONFIRMATION - 0.05)
        result.reasons.append(f"but its date is still ahead ({(thread.deadline or '')[:10]})")

    # --- 2b. the silence broke ---------------------------------------------
    # A silence thread's entire claim is "no reply since your last word". An
    # inbound message in that conversation after the silence started is the
    # claim's negation — definite by construction, whatever it says. This
    # matters most for messages that arrive *late*: Theo B's replies were
    # ingested five days after he sent them (the store was blind), and the
    # thread built on the blind spot stayed live until the draft writer
    # happened to read the conversation and call the summary inaccurate.
    if getattr(thread, "origin", None) == "silence":
        broke = _silence_broke(thread)
        if broke:
            confidence += 0.85
            definite_parts += 1
            result.parts.append("silence_broke")
            result.reasons.append(broke["reason"])
            result.evidence.append({"kind": "message", "ref_id": broke["message_id"]})

    # --- 3. you answered ---------------------------------------------------
    # v1.5's single most productive closer: 76 of its auto-closes came from the
    # user's own reply in the same conversation. Reused wholesale.
    replied = _user_replied_since(thread)
    if replied:
        confidence += 0.35
        definite_parts += 1
        result.parts.append("you_replied")
        result.reasons.append(replied["reason"])
        result.evidence.append({"kind": "message", "ref_id": replied["message_id"]})

    # --- 4. something that reads like a receipt ----------------------------
    # Deliberately weak and deliberately never sufficient. This is the shape of
    # signal that produced 37 rejections and 2 confirmations for items.
    receipt = _receipt_language(thread)
    if receipt:
        confidence += 0.15
        fuzzy_parts += 1
        result.parts.append("receipt_language")
        result.reasons.append(receipt["reason"])
        result.evidence.append({"kind": "message", "ref_id": receipt["message_id"]})

    result.confidence = round(min(confidence, 0.99), 3)
    # "Definite" means every contributing part was structural. One fuzzy part
    # is enough to make the whole thing a question rather than a decision.
    result.definite = definite_parts > 0 and fuzzy_parts == 0
    return result


def _all_items_closed(items) -> bool:
    return bool(items) and all(i.status in ("completed", "dismissed") for i in items)


def _watcher_confirmed(thread) -> Optional[Dict[str, Any]]:
    """Evidence a watcher attached, and which watcher went and got it.

    `deadline` watchers are excluded: one firing means the date is *coming*,
    which is the opposite of the thread being over. The rest — mail, messages,
    calendar — only fire when the specific thing they were told to look for
    turns up.

    Watcher-attached evidence is identified by the note `watchers.check`
    writes, which is the only marker distinguishing it from evidence the
    thread claimed on its own.
    """
    from . import watchers as watchers_mod

    watching = [
        w for w in db.thread_watchers(thread.id)
        if w.kind != watchers_mod.WatchKind.DEADLINE and w.fire_count > 0
    ]
    if not watching:
        return None

    found = [
        e for e in db.thread_evidence(thread.id)
        if (e.note or "").startswith("watcher: ")
    ]
    if not found:
        return None

    what = watching[0].what or "something it was watching for"
    return {
        "reason": (
            f"the watcher for {what.lower()} found "
            f"{'it' if len(found) == 1 else f'{len(found)} matches'}"
        ),
        "evidence": [{"kind": e.kind, "ref_id": e.ref_id} for e in found[:3]],
    }


def _silence_broke(thread) -> Optional[Dict[str, str]]:
    """An inbound message after the silence this thread was founded on —
    judged by message *timestamp*, not ingestion time, so a reply the store
    learned about late still counts from the moment it was actually sent."""
    conn = db.get_connection()
    for e in db.thread_evidence(thread.id):
        if e.kind != "message":
            continue
        founding = db.get_message(e.ref_id)
        if not founding:
            continue
        row = conn.execute(
            "SELECT id, timestamp, substr(text, 1, 60) AS snippet FROM messages "
            "WHERE conversation_id = ? AND is_from_user = 0 AND timestamp > ? "
            "ORDER BY timestamp LIMIT 1",
            (founding.conversation_id, founding.timestamp),
        ).fetchone()
        if row:
            return {
                "message_id": row["id"],
                "reason": f'they answered on {row["timestamp"][:10]}: "{(row["snippet"] or "").strip()}"',
            }
    return None


def _user_replied_since(thread) -> Optional[Dict[str, str]]:
    """Did the user say something *about this thread* in its conversation after
    it opened? For a thread that exists because someone is waiting on them,
    that is the loop closing.

    "About this" is the whole rule. It used to be any message at all, which
    reads a conversation as if it had one subject: the thread was opened, the
    user texted that person, therefore the thread is done. People text the same
    person about eleven other things. Live, this fired on "😒" and on "Where
    are you" as the case for closing a thread about buying a basketball hoop —
    both put to the user, both rejected — and paired with a date that had gone
    by it was enough to close an unpaid bill without asking.
    """
    opened = parse_iso(thread.opened_at)
    if not opened:
        return None
    conversations = set()
    subject = matcher.tokens(thread.title)
    for e in db.thread_evidence(thread.id):
        if e.kind == "message":
            message = db.get_message(e.ref_id)
            if message:
                conversations.add(message.conversation_id)
        elif e.kind == "item":
            item = db.get_item(e.ref_id)
            if item:
                if item.conversation_id:
                    conversations.add(item.conversation_id)
                # What the thread is tracking is part of what it is about: a
                # thread titled "Detroit water meter" is answered by a reply
                # mentioning the plumber its item names.
                subject |= matcher.tokens(f"{item.suggested_action or ''} {item.raw_text or ''}")

    subject -= matcher._GENERIC
    if not subject:
        return None      # nothing to be about — no reply can be evidence

    for conversation_id in conversations:
        for reply in db.user_replies_after(conversation_id, thread.opened_at):
            shared = matcher.tokens(reply.text) & subject
            if not shared:
                continue
            snippet = (reply.text or "").strip().replace("\n", " ")[:60]
            return {
                "message_id": reply.id,
                "reason": (
                    f'you replied "{snippet}" after this thread opened, '
                    f"mentioning {', '.join(sorted(shared)[:3])}"
                ),
            }
    return None


def _receipt_language(thread) -> Optional[Dict[str, str]]:
    """A message since the thread opened that reads like a confirmation and
    shares vocabulary with the thread's title."""
    from ..assistant import tools as assistant_tools

    title_tokens = matcher.tokens(thread.title)
    if not title_tokens:
        return None
    hits = assistant_tools.search_messages(
        query=thread.title, since=thread.opened_at, direction="from_them", limit=5
    )
    for hit in hits:
        text = f"{hit.get('subject') or ''}\n{hit.get('text') or ''}"
        if matcher._CANCELLATION_LANGUAGE.search(text):
            continue
        if not matcher._CONFIRMATION_LANGUAGE.search(text):
            continue
        shared = title_tokens & matcher.tokens(text)
        if shared - matcher._GENERIC:
            return {
                "message_id": hit["message_id"],
                "reason": f"a confirmation arrived mentioning {', '.join(sorted(shared)[:3])}",
            }
    return None


# ------------------------------------------------------------------- engine
def scan(reference: Optional[datetime] = None) -> Dict[str, Any]:
    """Look at every open thread and act on what the evidence says.

    Runs on the poller. Auto-closes only on definite evidence; anything else
    that clears the ask band is recorded as a question and left for the user.
    """
    now = reference or datetime.now(timezone.utc)
    closed, asked, scanned = [], [], 0

    for thread in db.list_threads(states=ThreadState.OPEN, reference=now):
        scanned += 1
        result = match(thread, now)
        resolution = result.resolution
        if resolution is None:
            continue
        if db.thread_closure_exists(thread.id, result.argument_key):
            continue      # already made this argument; don't make it twice

        record = db.save_thread_closure(
            thread_id=thread.id,
            confidence=result.confidence,
            reasons=result.reasons,
            evidence=result.evidence,
            resolution=resolution,
            evidence_key=result.argument_key,
        )
        if resolution == "auto_closed":
            from . import resolve as resolve_thread

            resolve_thread(thread.id, by="evidence")
            closed.append(thread.id)
            log.info("closed %r on evidence: %s", thread.title, "; ".join(result.reasons[:2]))
        else:
            asked.append(record["id"])

    return {"scanned": scanned, "auto_closed": len(closed), "needs_confirmation": len(asked)}


def confirm(closure_id: str) -> Optional[Any]:
    """The user says yes — the thread closes, and `resolved_by` records that
    evidence made the case even though a person signed it off."""
    record = db.get_thread_closure(closure_id)
    if not record or record["resolution"] != "needs_confirmation":
        return None
    db.resolve_thread_closure(closure_id, "confirmed")
    from . import resolve as resolve_thread

    return resolve_thread(record["thread_id"], by="evidence")


def reject(closure_id: str) -> Optional[Any]:
    """The user says no. The thread stays open and the rejection is kept —
    a wrong guess the user corrected is the most informative row in the table."""
    record = db.get_thread_closure(closure_id)
    if not record:
        return None
    db.resolve_thread_closure(closure_id, "rejected")
    return db.get_thread(record["thread_id"])
