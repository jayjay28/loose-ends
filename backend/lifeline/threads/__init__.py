"""Threads — the §v2 unit of the product: an open loop in the user's head.

Everything here is deliberately boring: plain functions over `db`, no LLM, no
framework. The intelligence arrives in step 4 (the worker loop) and rides these
same primitives; step 1 only has to make the object real, claimable, and able
to show its receipts.

Three rules this module enforces, because they are the spec's load-bearing
promises rather than implementation detail:

1. **Proposals never enter the main stack.** `open_threads()` is live+quiet.
   A proposal becomes a thread only when the user accepts it.
2. **The user overrules the system.** A user-set deadline is never overwritten
   by an inferred one; the reverse is always allowed.
3. **An inferred deadline carries its evidence.** `set_deadline(...,
   source="inferred")` without evidence is a guess, and is refused.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from .. import db
from ..models import (
    DeadlineSource,
    Evidence,
    Fact,
    Item,
    Thread,
    ThreadOrigin,
    ThreadState,
    now_iso,
    parse_iso,
)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _now(reference: Optional[datetime] = None) -> datetime:
    """Matches `signals._now` — every time-dependent read takes an injectable
    reference so tests never race the wall clock."""
    return reference or datetime.now(timezone.utc)


log = logging.getLogger(__name__)


class ThreadError(ValueError):
    """A caller (tool, endpoint, or CLI) asked for something the model forbids."""


# ----------------------------------------------------------------- create
def create(
    title: str,
    *,
    summary: str = "",
    origin: str = ThreadOrigin.USER,
    state: str = ThreadState.LIVE,
    key: Optional[str] = None,
    importance: float = 0.5,
    deadline: Optional[str] = None,
    deadline_source: Optional[str] = None,
    deadline_reason: Optional[str] = None,
    deadline_evidence: Optional[List[Dict[str, str]]] = None,
    evidence: Optional[Iterable[Evidence]] = None,
    contact_person_id: Optional[str] = None,
) -> Thread:
    """Declare a thread. `key` makes the creation idempotent for the system
    producers that run every poll cycle (the silence sweep); a user-declared
    thread has no key and is never deduped — if you say you're carrying it
    twice, you're carrying it twice."""
    title = (title or "").strip()
    if not title:
        raise ThreadError("a thread needs a title")
    if state not in ThreadState.ALL:
        raise ThreadError(f"unknown state {state!r}")
    if origin not in ThreadOrigin.ALL:
        raise ThreadError(f"unknown origin {origin!r}")
    if contact_person_id and not db.get_person(contact_person_id):
        raise ThreadError(f"unknown person {contact_person_id!r}")

    if key:
        existing = db.get_thread_by_key(key)
        if existing:
            return existing

    thread = Thread(
        title=title,
        summary=(summary or "").strip(),
        origin=origin,
        state=state,
        key=key,
        importance=_clamp(importance),
        deadline=deadline,
        deadline_source=deadline_source,
        deadline_reason=deadline_reason,
        deadline_evidence=list(deadline_evidence or []),
        contact_person_id=contact_person_id,
    )
    db.save_thread(thread)
    for e in evidence or []:
        e.thread_id = thread.id
        db.add_evidence(e)
    # §v2.8 phase 4 — a thread is bound to the things it is about, so the
    # plumber thread and the water-meter thread can finally see they share a
    # house. Failure costs nothing but the binding.
    try:
        from .. import world
        world.bind_thread(thread.id, f"{title} {summary or ''}",
                          person_id=contact_person_id)
    except Exception:
        log.exception("entity binding failed for %s", thread.id)
    return thread


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


# ----------------------------------------------------------------- update
def update(
    thread_id: str,
    *,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    state: Optional[str] = None,
    importance: Optional[float] = None,
    resolved_by: str = "user",
) -> Thread:
    thread = _require(thread_id)
    if title is not None:
        title = title.strip()
        if not title:
            raise ThreadError("a thread needs a title")
        thread.title = title
    if summary is not None:
        thread.summary = summary.strip()
    if importance is not None:
        thread.importance = _clamp(importance)
    if state is not None:
        _transition(thread, state, resolved_by=resolved_by)
    return db.save_thread(thread)


def _transition(thread: Thread, state: str, resolved_by: str = "user",
                reference: Optional[datetime] = None) -> None:
    if state not in ThreadState.ALL:
        raise ThreadError(f"unknown state {state!r}")
    if state == thread.state:
        return
    if state == ThreadState.PROPOSED:
        # One-way door. A thread on the stack was acknowledged by the user;
        # demoting it back to a guess would quietly un-say that.
        raise ThreadError("a thread cannot be demoted back to a proposal")
    thread.state = state
    if state == ThreadState.RESOLVED:
        # Injectable so a caller driving a frozen clock stamps a consistent
        # time. Without it `sweep_resolved` compared a wall-clock stamp
        # against a frozen cutoff, and the archive test passed or failed
        # purely on what time of day it happened to run.
        thread.resolved_at = (reference.isoformat(timespec="seconds")
                              if reference else now_iso())
        thread.resolved_by = resolved_by
    elif state in (ThreadState.LIVE, ThreadState.QUIET):
        # Reopening: the resolution is no longer true, so it stops being on
        # the record. A stale resolved_at reads as "closed" in every receipt.
        thread.resolved_at = None
        thread.resolved_by = None


def resolve(thread_id: str, by: str = "user", evidence: Optional[Evidence] = None,
            reference: Optional[datetime] = None) -> Thread:
    """Close a loop. `by` is user or evidence — the spec's two closers. Step 5
    supplies the evidence path; step 1 gives it somewhere to land."""
    if by not in ("user", "evidence"):
        raise ThreadError("resolved_by must be 'user' or 'evidence'")
    thread = _require(thread_id)
    if evidence is not None:
        evidence.thread_id = thread.id
        db.add_evidence(evidence)
    _transition(thread, ThreadState.RESOLVED, resolved_by=by, reference=reference)
    saved = db.save_thread(thread)
    if by == "user":
        # §v2 7b — the swipe is the training signal. Only the user's own close
        # teaches anything: evidence closing a thread says the world moved on,
        # not that the user cared.
        from ..ranking import learning

        learning.record_thread("resolved", saved)
    return saved


def quiet(thread_id: str) -> Thread:
    """"Right thread, wrong moment." Keeps working, stops surfacing — and
    raises the interruption bar, because what was wrong was the timing."""
    thread = update(thread_id, state=ThreadState.QUIET)
    from ..notifications import interruption

    interruption.quieted()
    return thread


def dig_in(thread_id: str, step: float = 0.25) -> Thread:
    """"This matters more than I judged." Raises the thread's importance, and
    lowers the bar — the user is asking to hear sooner, not just about this."""
    thread = _require(thread_id)
    raised = _clamp(thread.importance + step)
    updated = update(
        thread_id, importance=raised,
        state=ThreadState.LIVE if thread.state == ThreadState.QUIET else None,
    )
    from ..notifications import interruption
    from ..ranking import learning

    learning.record_thread("dug_in", updated)
    interruption.dug_in()
    return updated


def accept_proposal(thread_id: str) -> Thread:
    """A tap promotes a proposal into a live thread. This is the only door
    into the main stack for anything the system proposed."""
    thread = _require(thread_id)
    if thread.state != ThreadState.PROPOSED:
        raise ThreadError(f"thread {thread_id} is not a proposal (state={thread.state})")
    _transition(thread, ThreadState.LIVE)
    return db.save_thread(thread)


def dismiss_proposal(thread_id: str) -> Thread:
    """Ignoring a proposal archives it rather than deleting it — the refusal
    is signal, the same way a dismissed fact is."""
    thread = _require(thread_id)
    if thread.state != ThreadState.PROPOSED:
        raise ThreadError(f"thread {thread_id} is not a proposal (state={thread.state})")
    _transition(thread, ThreadState.ARCHIVED)
    return db.save_thread(thread)


# --------------------------------------------------------------- evidence
def claim(
    thread_id: str,
    ref_id: str,
    *,
    kind: str = "item",
    role: str = "claimed",
    note: Optional[str] = None,
) -> Evidence:
    """Attach a row to a thread. The row is not moved or consumed — one item
    can serve several threads, which is why evidence is a join table."""
    _require(thread_id)
    if not _resolve_ref(kind, ref_id):
        raise ThreadError(f"no {kind} {ref_id}")
    return db.add_evidence(
        Evidence(thread_id=thread_id, kind=kind, ref_id=ref_id, role=role, note=note)
    )


def unclaim(thread_id: str, ref_id: str, kind: str = "item") -> bool:
    return db.remove_evidence(thread_id, kind, ref_id)


def _resolve_ref(kind: str, ref_id: str) -> Optional[Dict[str, Any]]:
    """One evidence reference, rendered for a human. This is the receipt: an
    inferred deadline that can't point at the sentence it came from is a guess
    wearing a date's clothes."""
    if kind == "item":
        item = db.get_item(ref_id)
        if not item:
            return None
        return {
            "kind": "item",
            "ref_id": item.id,
            "title": item.entities.item or item.suggested_action or item.raw_text[:80],
            "text": (item.raw_text or "")[:400],
            "person": item.person,
            "person_id": item.person_id,
            "timestamp": item.timestamp,
            "date": item.entities.date,
            "status": item.status,
            "source": item.source,
        }
    if kind == "message":
        message = db.get_message(ref_id)
        if not message:
            return None
        person = db.get_person(message.person_id) if message.person_id else None
        return {
            "kind": "message",
            "ref_id": message.id,
            "title": str(message.metadata.get("subject") or "")[:80] or (message.text or "")[:80],
            "text": (message.text or "")[:400],
            "person": "You" if message.is_from_user else (person.display_name if person else "someone"),
            "person_id": message.person_id,
            "timestamp": message.timestamp,
            "date": None,
            "status": None,
            "source": message.source,
        }
    if kind == "calendar_event":
        event = next((e for e in db.list_calendar_events() if e.id == ref_id), None)
        if not event:
            return None
        return {
            "kind": "calendar_event",
            "ref_id": event.id,
            "title": event.summary,
            "text": (event.description or event.location or "")[:400],
            "person": None,
            "person_id": None,
            "timestamp": event.start_at,
            "date": event.start_at,
            "status": event.status,
            "source": "calendar",
        }
    raise ThreadError(f"unknown evidence kind {kind!r}")


def _tombstone(kind: str, ref_id: str) -> Dict[str, Any]:
    """A receipt that silently loses a line is worse than one that says the
    row is gone."""
    return {
        "kind": kind,
        "ref_id": ref_id,
        "title": "(missing)",
        "text": "",
        "person": None,
        "person_id": None,
        "timestamp": None,
        "date": None,
        "status": "missing",
        "source": None,
    }


def evidence_for(thread_id: str) -> List[Dict[str, Any]]:
    """Every claimed row, resolved and readable."""
    out: List[Dict[str, Any]] = []
    for e in db.thread_evidence(thread_id):
        resolved = _resolve_ref(e.kind, e.ref_id) or _tombstone(e.kind, e.ref_id)
        resolved["role"] = e.role
        resolved["note"] = e.note
        resolved["linked_at"] = e.linked_at
        out.append(resolved)
    return out


# --------------------------------------------------------------- deadline
def set_deadline(
    thread_id: str,
    date: Optional[str],
    *,
    source: str = DeadlineSource.INFERRED,
    evidence: Optional[List[Dict[str, str]]] = None,
    reason: Optional[str] = None,
    claim_evidence: bool = True,
) -> Thread:
    """Give a thread a due date, with its provenance.

    An inferred deadline MUST name the evidence that implied it. A user-set one
    needs nothing — the user is the evidence. And an inference never overwrites
    a date the user set: the spec puts the user above the system, and this is
    the one place the system would quietly disagree.
    """
    if source not in DeadlineSource.ALL:
        raise ThreadError(f"deadline source must be one of {DeadlineSource.ALL}")
    thread = _require(thread_id)

    if date is None:
        if source == DeadlineSource.INFERRED and thread.deadline_source == DeadlineSource.USER:
            raise ThreadError("cannot clear a user-set deadline by inference")
        thread.deadline = None
        thread.deadline_source = None
        thread.deadline_reason = None
        thread.deadline_evidence = []
        return db.save_thread(thread)

    refs = [dict(r) for r in (evidence or [])]
    if source == DeadlineSource.INFERRED:
        if not refs:
            raise ThreadError("an inferred deadline must carry the evidence that implied it")
        for ref in refs:
            kind, ref_id = ref.get("kind", "item"), ref.get("ref_id", "")
            if not _resolve_ref(kind, ref_id):
                raise ThreadError(f"deadline evidence {kind} {ref_id} does not exist")
        if thread.deadline_source == DeadlineSource.USER:
            raise ThreadError("thread has a user-set deadline; inference does not overrule the user")

    thread.deadline = date
    thread.deadline_source = source
    thread.deadline_reason = (reason or "").strip() or None
    thread.deadline_evidence = refs
    db.save_thread(thread)

    # The rows that implied the date are, by definition, evidence for the
    # thread. Claiming them keeps the receipt reachable from one place.
    if claim_evidence:
        for ref in refs:
            try:
                claim(thread.id, ref["ref_id"], kind=ref.get("kind", "item"))
            except ThreadError:      # already gone; the receipt keeps the ref
                continue
    return thread


def deadline_receipt(thread_id: str) -> Dict[str, Any]:
    """"Where did this date come from?" — answerable, always."""
    thread = _require(thread_id)
    return {
        "thread_id": thread.id,
        "deadline": thread.deadline,
        "source": thread.deadline_source,
        "reason": thread.deadline_reason,
        "evidence": [
            _resolve_ref(r.get("kind", "item"), r.get("ref_id", ""))
            or _tombstone(r.get("kind", "item"), r.get("ref_id", ""))
            for r in thread.deadline_evidence
        ],
    }


# ------------------------------------------------------------ read/search
def read_state(thread_id: str) -> Dict[str, Any]:
    """Everything a thread knows about itself.

    The audit's finding: tier 4 was all writes, so the worker loop would
    re-investigate from zero every pass. This is the read that stops that.
    """
    thread = _require(thread_id)
    evidence = evidence_for(thread.id)
    return {
        "thread_id": thread.id,
        "title": thread.title,
        "summary": thread.summary,
        "state": thread.state,
        "origin": thread.origin,
        "importance": thread.importance,
        "deadline": thread.deadline,
        "deadline_source": thread.deadline_source,
        "deadline_reason": thread.deadline_reason,
        "opened_at": thread.opened_at,
        "resolved_at": thread.resolved_at,
        "resolved_by": thread.resolved_by,
        "evidence_count": len(evidence),
        "evidence": evidence[:25],
        # Named so the model can't mistake absence for "nothing to do here".
        "findings": [
            {"kind": f["kind"], "headline": f["headline"], "when": f["created_at"]}
            for f in findings_for(thread.id)[:10]
        ],
        "watchers": [
            {"id": w.id, "kind": w.kind, "what": w.what,
             "every_minutes": w.cadence_minutes, "until": w.until,
             "times_fired": w.fire_count}
            for w in _watchers_for(thread.id)
        ],
    }


_WORD = re.compile(r"[a-z0-9']+")


def search(
    query: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    """Find the thread an arriving piece of evidence belongs to.

    Scores over title, summary, and the text of claimed evidence — a hotel
    confirmation says "San Juan", not "Puerto Rico work trip", so title-only
    matching would miss the case the spec is built around. No match on a
    non-empty query returns nothing; that is an honest answer here, unlike in
    `search_calendar`, because "which thread owns this?" has a real "none".
    """
    states = [state] if state else list(ThreadState.OPEN)
    if state == "all":
        states = list(ThreadState.ALL)
    threads = db.list_threads(states=states)
    terms = [t for t in _WORD.findall((query or "").lower()) if len(t) > 2]
    if not terms:
        return [_summarise(t) for t in threads[:limit]]

    scored: List[tuple] = []
    for thread in threads:
        hay = f"{thread.title} {thread.summary}".lower()
        title_hits = sum(1 for t in terms if t in hay)
        body = " ".join(
            f"{e.get('title') or ''} {e.get('text') or ''} {e.get('person') or ''}"
            for e in evidence_for(thread.id)
        ).lower()
        body_hits = sum(1 for t in terms if t in body)
        score = title_hits * 2 + body_hits
        if score:
            scored.append((score, title_hits, thread))
    scored.sort(key=lambda s: (-s[0], -s[1]))
    return [_summarise(t, match=score) for score, _, t in scored[:limit]]


def _summarise(thread: Thread, match: Optional[int] = None) -> Dict[str, Any]:
    out = {
        "thread_id": thread.id,
        "title": thread.title,
        "summary": thread.summary[:200],
        "state": thread.state,
        "origin": thread.origin,
        "deadline": thread.deadline,
        "importance": thread.importance,
        "evidence_count": len(db.thread_evidence(thread.id)),
    }
    if match is not None:
        out["match_score"] = match
    return out


# --------------------------------------------------------------- promote
def promote_item(
    item_id: str,
    *,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    state: str = ThreadState.LIVE,
) -> Thread:
    """Turn a surfaced card into a loop the user is carrying.

    The item is *not* closed or consumed — it becomes the thread's founding
    evidence, which is exactly what the spec means by "items become evidence".
    Promoting twice returns the existing thread rather than splitting the loop.
    """
    item = db.get_item(item_id)
    if not item:
        raise ThreadError(f"no item {item_id}")

    existing = [
        t for t in db.threads_claiming("item", item.id)
        if t.origin == ThreadOrigin.PROMOTED and t.state not in ThreadState.CLOSED
    ]
    if existing:
        return existing[0]

    thread = create(
        title=title or _title_for_item(item),
        summary=summary or (item.raw_text or "")[:400],
        origin=ThreadOrigin.PROMOTED,
        state=state,
        evidence=[Evidence(kind="item", ref_id=item.id, role="founding")],
    )

    # An item that already carries a date hands the thread a deadline, with the
    # item itself as the receipt. This is the whole inferred-deadline story,
    # and it costs nothing here because extraction already did the work.
    if item.entities.date:
        set_deadline(
            thread.id,
            item.entities.date,
            source=DeadlineSource.INFERRED,
            evidence=[{"kind": "item", "ref_id": item.id}],
            reason=f"{item.person}: {(item.entities.item or item.raw_text or '')[:100]}",
        )
        thread = _require(thread.id)
    return thread


def _title_for_item(item: Item) -> str:
    """A thread title is the product's whole surface, so prefer the extracted
    entity ("kids investment account") over the raw sentence."""
    for candidate in (item.entities.item, item.suggested_action, item.raw_text):
        text = (candidate or "").strip()
        if text:
            return text[:80]
    return f"Open loop with {item.person}"


# --------------------------------------------------------------- the lane
# §v2 step 2. The stack is the product's main surface, so what a lane *looks
# like* is decided here and not on the client: `Theme.swift` already states the
# rule ("driven entirely by the server's interruption level so the client never
# re-decides urgency"), and a thread's urgency should obey it too.


class LaneState:
    """The stripe down the left of a lane, per `v2-threads-ui.html`."""

    HOT = "hot"        # ember — needs you today
    WARM = "warm"      # gold  — carries a deadline
    LIVE = "live"      # teal  — running
    IDLE = "idle"      # grey  — quiet, nothing moving
    DONE = "done"      # olive — resolved, struck through

    ALL = (HOT, WARM, LIVE, IDLE, DONE)


# "Closed threads stay one day, struck through, then archive. Resolution should
# be visible, not vanish." A pile that only ever grows is the failure mode the
# whole product is built against, so the moment it shrinks has to be seen.
RESOLVED_VISIBLE_HOURS = 24


def lane_state(thread: Thread, reference: Optional[datetime] = None) -> str:
    from ..extraction.dates import days_until

    now = _now(reference)
    if thread.state in ThreadState.CLOSED:
        return LaneState.DONE

    days = days_until(thread.deadline, now)
    # The -1 lower bound is `signals.deadline_pressure`'s own boundary: a date
    # up to a day past still scores as "due within 24 hours". Beyond that the
    # date is gone and stops being pressure — it falls through to live/idle.
    if days is not None and -1 <= days <= 1:
        return LaneState.HOT
    if days is not None and days > 1:
        return LaneState.WARM
    if thread.state == ThreadState.QUIET:
        return LaneState.IDLE
    return LaneState.LIVE


class Status:
    """§v2.7 — what has happened to this thread, in one word.

    `lane` says how urgent a thread is; this says what state the *system* has
    it in, which is a different question and until now an invisible one. A
    thread added a minute ago and a thread the worker checked an hour ago and
    found nothing rendered identically — both silent — so silence meant both
    "nothing to report" and "nobody has looked".

    Ordered by what the reader needs first: a passed date outranks a question
    outranks never having been looked at. `NONE` is the ordinary case and gets
    no label; most threads must return it, or the labels stop meaning anything.

    **"Watching" is deliberately not here.** It was, for one revision, and on
    the live stack it labelled nine threads out of eleven — a watcher is what
    the worker sets on almost every pass, so the word described the normal
    condition of a thread rather than anything worth noticing. A status that
    fits everything signals nothing, and five colours that all appear at once
    is the dashboard `Theme.swift` warns about. Watchers stay visible where
    they already are, on the detail screen, counted.
    """

    OVERDUE = "overdue"        # a date has passed
    NEEDS_YOU = "needs_you"    # blocked on an answer only the user has
    QUEUED = "queued"          # added, never worked
    FINISHED = "finished"      # resolved, or closure thinks it is
    NONE = "none"

    # Kept beside the constants so the client and `Theme.swift` cannot drift
    # apart about which word means which colour.
    LABEL = {
        OVERDUE: "Overdue",
        NEEDS_YOU: "Needs you",
        QUEUED: "Queued",
        FINISHED: "Looks finished",
    }


def status(thread: Thread, reference: Optional[datetime] = None) -> str:
    """One word for what the system has done with this thread."""
    from ..extraction.dates import days_until

    if thread.state in ThreadState.CLOSED:
        return Status.FINISHED

    days = days_until(thread.deadline, _now(reference))
    if days is not None and days < 0:
        return Status.OVERDUE

    # Never worked. `last_worked_at` is written by the worker on every pass, so
    # NULL is unambiguous — no backfill needed and no heuristic involved.
    if not getattr(thread, "last_worked_at", None):
        return Status.QUEUED

    # A move the worker could not finish because it needs the user. Read off
    # the thread's live findings rather than recomputed, so the label and the
    # card underneath it always agree.
    for finding in db.thread_findings(thread.id):
        if finding.kind == "action" and getattr(finding, "blocked_reason", None):
            return Status.NEEDS_YOU
    return Status.NONE


class Tier:
    """§v2.1 — where a thread sits on the front page.

    The stack used to be fourteen identical cards because the only thing that
    varied was a 3pt colour rail, and `importance` was 0.5 for every thread in
    the live database — so the client had nothing to build a hierarchy out of
    even if it had wanted one.

    Decided server-side for the same reason `lane` is: urgency is a judgement
    about the user's whole situation, and the client does not have the evidence
    to re-make it.
    """

    LEAD = "lead"        # one story, the top of the page
    BRIEF = "brief"      # above the fold, with a deck
    INDEX = "index"      # a quiet one-line list
    QUIET = "quiet"      # hushed, or nothing moving
    CLOSED = "closed"


# How long a thread the user declared themselves is held above the fold, and
# the floor it is held at. The floor sits just over `tiers`' brief threshold
# and well under its lead bar: declaring something guarantees it is *seen*
# today, and cannot buy it the top of the page. The 0.05 is a tie-break, so
# two things declared this morning order by which was said last.
DECLARED_WINDOW_H = 24.0
DECLARED_FLOOR = 0.30
DECLARED_FLOOR_BONUS = 0.05


def pressure(thread: Thread, reference: Optional[datetime] = None) -> float:
    """How much this thread is pressing on the user right now, 0-1.

    Not the same as learned importance, and deliberately kept separate from it:
    importance is what the user tends to care about, pressure is what today
    demands. A thread can be important and calm.
    """
    from ..extraction.dates import days_until
    from ..ranking import learning

    now = _now(reference)
    score = 0.0

    days = days_until(thread.deadline, now)
    if days is not None:
        if days < 0:
            score += 0.55           # already missed — the loudest thing there is
        elif days <= 1:
            score += 0.5
        elif days <= 7:
            score += 0.3
        else:
            score += 0.1

    # A staged move means the work is done and only the user is missing. That
    # is worth more than a thread that merely has news.
    for finding in db.thread_findings(thread.id):
        if finding.kind == "action" and not finding.dismissed_at:
            score += 0.25
            break

    if thread.state == ThreadState.QUIET:
        score -= 0.4

    # Learned importance rides along at a fraction, so the day's demands lead
    # and the user's habits break ties rather than overruling a deadline.
    score += (learning.thread_importance(thread) - 0.5) * 0.3

    # A thread the user declared today carries the weight of having been said
    # out loud. Nothing above knows the user *chose* to type it: a fresh
    # declaration with no deadline scored 0.0 and landed in the index, so the
    # product ranked its own clearest signal last, and the thing you just
    # added was the hardest thing on the page to find.
    #
    # A floor rather than a term, because a term is the wrong shape. Adding
    # a decaying 0.3 drops under the brief threshold minutes after you type,
    # and adding it to a real deadline lets "I declared this" outrank an
    # overdue bill. A floor says only what is true — *this is at least worth
    # seeing today* — and leaves a thread that has earned more alone.
    if thread.state != ThreadState.QUIET:
        score = max(score, _declaration_floor(thread, now))

    return round(max(0.0, min(1.0, score)), 4)


def _declaration_floor(thread: Thread, now: datetime) -> float:
    """The floor a user-declared thread holds for its first day, decaying so
    the most recently declared sorts above the rest of today's."""
    if thread.origin != ThreadOrigin.USER:
        return 0.0
    opened = parse_iso(thread.opened_at)
    if opened is None:
        return 0.0
    # Clamped, so a clock skew that puts `opened_at` slightly ahead of now
    # reads as "just declared" rather than silently forfeiting the floor.
    hours = max(0.0, (now - opened).total_seconds() / 3600.0)
    if hours >= DECLARED_WINDOW_H:
        return 0.0
    return DECLARED_FLOOR + DECLARED_FLOOR_BONUS * (1 - hours / DECLARED_WINDOW_H)


def tiers(threads_in: List[Thread], reference: Optional[datetime] = None) -> Dict[str, str]:
    """Assign every thread a tier. Returns {thread_id: tier}.

    Exactly one lead, and only when something has genuinely earned it — a page
    with a lead story every day regardless of whether one exists is a page that
    has taught the user to ignore its lead.
    """
    now = _now(reference)
    scored = []
    out: Dict[str, str] = {}

    for thread in threads_in:
        if thread.state in ThreadState.CLOSED:
            out[thread.id] = Tier.CLOSED
            continue
        if thread.state == ThreadState.QUIET:
            out[thread.id] = Tier.QUIET
            continue
        scored.append((pressure(thread, now), thread))

    scored.sort(key=lambda pair: -pair[0])
    for rank, (score, thread) in enumerate(scored):
        if rank == 0 and score >= 0.55:
            out[thread.id] = Tier.LEAD
        elif score >= 0.3:
            out[thread.id] = Tier.BRIEF
        else:
            out[thread.id] = Tier.INDEX
    return out


def why(thread: Thread, reference: Optional[datetime] = None) -> Optional[Dict[str, str]]:
    """§v3 (Loose Ends) — the reason chip: the pressure score, in words.

    A card that moves without saying which force moved it reads as a shuffle;
    the chip is each `pressure` term made legible, loudest first, exactly one
    per card. `kind` is the vocabulary the client maps to a tone; `text` is
    what it prints. Most quiet placements return None — a chip on every card
    would be the status-word dashboard `Status` already refused to be.
    """
    from ..extraction.dates import days_until

    now = _now(reference)

    if thread.state in ThreadState.CLOSED:
        return {"kind": "tied", "text": "tied off"}
    if thread.state == ThreadState.QUIET:
        return None

    days = days_until(thread.deadline, now)
    if days is not None:
        if days < 0:
            return {"kind": "overdue", "text": "overdue"}
        if days == 0:
            return {"kind": "due", "text": "due today"}
        if days == 1:
            return {"kind": "due", "text": "due tomorrow"}
        if days <= 7:
            when = parse_iso(thread.deadline)
            if when is not None:
                return {"kind": "due", "text": f"due {when.astimezone().strftime('%a')}"}
            return {"kind": "due", "text": f"due in {days}d"}

    for finding in db.thread_findings(thread.id):
        if finding.kind == "action" and not finding.dismissed_at:
            return {"kind": "move", "text": "move ready"}

    if _declaration_floor(thread, now) > 0:
        return {"kind": "new", "text": "new today"}

    # Nothing pressing: say how long it has sat, once that's worth a word.
    touched = parse_iso(thread.updated_at) or parse_iso(thread.opened_at)
    if touched is not None:
        waited = int((now - touched).total_seconds() // 86400)
        if waited >= 2:
            return {"kind": "waited", "text": f"waited {waited}d"}
    return None


def activity(thread_id: str) -> List[Dict[str, Any]]:
    """The marks on a lane's activity track — the system's work, over time.

    Blue is a finding, green an action it prepared, grey "I looked and found
    nothing" — and that last one is the honest mark the track exists for.
    """
    marks = [{"at": e.linked_at, "kind": "evidence"} for e in db.thread_evidence(thread_id)]
    for f in db.thread_findings(thread_id):
        # A "nothing" finding is grey, like evidence: the system did work and
        # it came to nothing, which the user is entitled to see.
        marks.append({"at": f.created_at, "kind": f.kind if f.kind != "nothing" else "checked"})
    marks.sort(key=lambda m: m["at"] or "")
    return marks


def unseen_count(thread: Thread) -> int:
    """What the "N NEW" badge counts: evidence that arrived *after the thread
    became something the user knows about*.

    The watermark is `last_seen_at`, falling back to `opened_at` for a thread
    never opened. That fallback is the whole point. Counting every row on an
    unopened thread is defensible in isolation and useless in practice: on the
    first run it made the header read "15 running · 15 need you", which says
    exactly nothing. Founding evidence isn't news about a thread — it *is* the
    thread. The badge is for what landed since.
    """
    watermark = parse_iso(thread.last_seen_at) or parse_iso(thread.opened_at)
    if watermark is None:
        return 0
    return sum(
        1 for e in db.thread_evidence(thread.id)
        if (parse_iso(e.linked_at) or EPOCH) > watermark
    )


SUBTITLE_CHARS = 120


def _clip(text: str, limit: int = SUBTITLE_CHARS) -> str:
    """Trim to a word boundary and mark the cut. A hard slice reads as a bug —
    the lane rendered "confirm details befo" on the first run — and the client
    can't repair it, because by then the sentence is already gone."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0].rstrip(",;:—- ")
    return f"{head or text[:limit]}…"


def subtitle(thread: Thread) -> Optional[str]:
    """The lane's one line under the title. The thread's own summary when it has
    one, else its most recent piece of evidence — never an empty row."""
    text = _clip(thread.summary or "")
    if text:
        return text
    rows = evidence_for(thread.id)
    if not rows:
        return None
    latest = max(rows, key=lambda e: e.get("linked_at") or "")
    return _clip(latest.get("title") or latest.get("text") or "") or None


def mark_seen(thread_id: str, reference: Optional[datetime] = None) -> Thread:
    """The user opened it, so nothing in it is new any more."""
    thread = _require(thread_id)
    thread.last_seen_at = _now(reference).isoformat(timespec="seconds")
    return db.save_thread(thread)


def stack(reference: Optional[datetime] = None) -> List[Thread]:
    """The main view: what you're carrying, plus what you just put down.

    Recently-resolved threads ride along so the client can strike them through
    for a day. They sort last — done is done — but they are *there*, which is
    the point.
    """
    now = _now(reference)
    cutoff = now - timedelta(hours=RESOLVED_VISIBLE_HOURS)
    open_now = db.list_threads(states=ThreadState.OPEN, reference=now)
    recent = [
        t for t in db.list_threads(states=[ThreadState.RESOLVED], reference=now)
        if (parse_iso(t.resolved_at) or EPOCH) >= cutoff
    ]
    recent.sort(key=lambda t: t.resolved_at or "", reverse=True)
    return open_now + recent


def sweep_resolved(reference: Optional[datetime] = None) -> int:
    """Archive resolutions once their day in the light is over. Runs on the
    poller, so the stack empties itself without the user tidying."""
    now = _now(reference)
    cutoff = now - timedelta(hours=RESOLVED_VISIBLE_HOURS)
    archived = 0
    for thread in db.list_threads(states=[ThreadState.RESOLVED], reference=now):
        resolved_at = parse_iso(thread.resolved_at)
        if resolved_at is None or resolved_at < cutoff:
            # A resolved thread with no timestamp is malformed; archiving it is
            # the conservative read (it was closed, we just can't date it).
            thread.state = ThreadState.ARCHIVED
            db.save_thread(thread)
            archived += 1
    if archived:
        log.info("archived %d resolved thread(s) past their day", archived)
    return archived


def counts(reference: Optional[datetime] = None) -> Dict[str, int]:
    """The header line: "7 running · 2 need you"."""
    now = _now(reference)
    open_now = db.list_threads(states=ThreadState.OPEN, reference=now)
    return {
        "running": len(open_now),
        "needs_you": sum(
            1 for t in open_now
            if lane_state(t, now) == LaneState.HOT or unseen_count(t) > 0
        ),
    }


# ---------------------------------------------------------------- findings
def make_finding(
    thread_id: str,
    *,
    kind: str = "finding",
    headline: str,
    body: str = "",
    importance: float = 0.5,
    evidence: Optional[List[Dict[str, str]]] = None,
    loop_run_id: Optional[str] = None,
    move_kind: Optional[str] = None,
    steps: Optional[List[Dict[str, str]]] = None,
    needs: Optional[List[str]] = None,
    blocked_reason: Optional[str] = None,
    facts: Optional[List[Dict[str, str]]] = None,
) -> "Finding":
    """Build a finding for a thread. Not saved — the worker attaches the run id
    first, because a finding whose provenance can't be opened is an assertion."""
    from ..models import Finding

    return Finding(
        thread_id=thread_id,
        kind=kind,
        headline=(headline or "").strip()[:200],
        body=(body or "").strip(),
        importance=_clamp(importance),
        evidence=list(evidence or []),
        facts=list(facts or []),
        loop_run_id=loop_run_id,
        move_kind=move_kind,
        steps=list(steps or []),
        needs=list(needs or []),
        blocked_reason=blocked_reason,
    )


def findings_for(thread_id: str, include_dismissed: bool = False) -> List[Dict[str, Any]]:
    """A thread's findings, resolved for reading — each with its evidence and
    the run that produced it."""
    out = []
    for f in db.thread_findings(thread_id, include_dismissed=include_dismissed):
        out.append({
            "id": f.id,
            "kind": f.kind,
            "headline": f.headline,
            "body": f.body,
            "importance": f.importance,
            "created_at": f.created_at,
            "loop_run_id": f.loop_run_id,
            "move_kind": f.move_kind,
            # Stored as [{"text": ...}] so a step can grow fields later without
            # a migration; flattened here because the wire wants strings.
            "steps": [s.get("text", "") if isinstance(s, dict) else str(s)
                      for s in f.steps],
            "needs": f.needs,
            "blocked_reason": f.blocked_reason,
            # §v2.3 — whether this is the thread's current picture or its
            # history. The client leads with what is current and collapses the
            # rest; without the flag every pass the worker ever made arrives
            # with equal weight, which is what made the screen unreadable.
            "superseded": f.superseded_at is not None,
            # §v2.3 — verified figures with sources. The client renders these
            # as rows; before they existed the same numbers were only ever a
            # paragraph the user had to read and re-derive.
            "facts": f.facts,
            "evidence": [
                _resolve_ref(r.get("kind", "item"), r.get("ref_id", ""))
                or _tombstone(r.get("kind", "item"), r.get("ref_id", ""))
                for r in f.evidence
            ],
        })
    return out


def _watchers_for(thread_id: str):
    from .watchers import for_thread

    return for_thread(thread_id)


def set_autonomy(thread_id: str, ceiling: str) -> Thread:
    """The ladder's ceiling, per thread. User-set only — see
    `registry.scoped_for` for why learning may lower it but never raise it."""
    from ..models import Autonomy

    if ceiling not in Autonomy.ALL:
        raise ThreadError(f"autonomy must be one of {Autonomy.ALL}")
    thread = _require(thread_id)
    thread.autonomy = ceiling
    return db.save_thread(thread)


def set_contact(thread_id: str, person_id: Optional[str]) -> Thread:
    """Who to write to when this thread needs a message. `None` clears it.

    Validated against the people table rather than accepted as free text:
    a contact that does not resolve is worse than no contact, because the
    writer would take it as an instruction and draft to nobody.
    """
    thread = _require(thread_id)
    if person_id:
        if not db.get_person(person_id):
            raise ThreadError(f"unknown person {person_id!r}")
        thread.contact_person_id = person_id
    else:
        thread.contact_person_id = None
    return db.save_thread(thread)


# ---------------------------------------------------------- drafting context
def draft_brief(thread_id: str) -> Dict[str, Any]:
    """Everything a writer needs to draft for this thread.

    This is the difference the spec is after. Today's replies are written
    during *extraction*, from a single message, with no idea what loop it
    belongs to — which is how 33 items in the live database ended up drafted as
    "yep, I'll take care of it", four of them addressed to billing robots at
    American Water, Capital One, Fidelity and a Marriott front desk.
    A draft belongs to a thread and should be written when it's needed, with
    everything the thread knows.
    """
    thread = _require(thread_id)
    evidence = evidence_for(thread.id)

    # Who this thread concerns, discovered through its evidence. A thread the
    # *user* declared has no evidence at all, which is why the explicit contact
    # goes in first: without it the writer had nobody to address and had to
    # invent a recipient or refuse.
    people: Dict[str, Dict[str, Any]] = {}
    ids = []
    if thread.contact_person_id:
        ids.append(thread.contact_person_id)
    ids.extend(row.get("person_id") for row in evidence)

    for pid in ids:
        if not pid or pid in people:
            continue
        person = db.get_person(pid)
        if not person:
            continue
        from ..ranking import relationships

        people[pid] = {
            # The writer should not have to guess which of several people the
            # user meant when they said who to contact.
            "is_the_contact": pid == thread.contact_person_id,
            "person_id": pid,
            "name": person.display_name,
            "handles": person.handles,
            "tie": relationships.describe(pid),
            # Surfaced so the writer can judge whether a reply is even a thing
            # that happens here, rather than a regex deciding for it.
            "looks_automated": relationships._is_service(pid),
            "you_have_written_to_them": _has_user_written(pid),
        }

    return {
        "thread_id": thread.id,
        "title": thread.title,
        "summary": thread.summary,
        "state": thread.state,
        "deadline": thread.deadline,
        "deadline_reason": thread.deadline_reason,
        "contact_person_id": thread.contact_person_id,
        "people": list(people.values()),
        "evidence": [
            {
                "kind": e["kind"],
                "from": e.get("person"),
                "when": e.get("timestamp"),
                "what": e.get("title"),
                "text": e.get("text"),
            }
            for e in evidence[:12]
        ],
        # The user's own words about this thread, and the highest-authority
        # thing in the brief: everything else here is the system's reading of
        # the evidence, and this is the user telling it that reading was wrong.
        # Empty on almost every thread, which is the point — when it is not
        # empty, someone took the trouble to type it.
        "what_you_told_me": [f.statement for f in corrections(thread.id)],
    }


# ----------------------------------------------------------- corrections
#
# What the user told this thread after the system got it wrong.
#
# Until v2.4 the only feedback a move could receive was "Not this", which
# nudged `move:<kind>` toward zero and told the worker nothing about *why*.
# Three different intents — "I already did it", "you misread the job", "that
# reason is stale" — all collapsed into the same narrowing signal, and
# `may_propose` only ever closes doors. On the pajamas thread it would have
# taught the opposite of what the user meant: `decide` was the right shape,
# and the correction was that the choosing is his, not something to ask about.

CORRECTION = "thread"


def corrections(thread_id: str) -> List[Fact]:
    """The user's own corrections on this thread, newest first."""
    return db.list_facts(subject_type=CORRECTION, subject_id=thread_id)


def correct(thread_id: str, statement: str) -> Fact:
    """Record what the user says the system got wrong here."""
    _require(thread_id)
    return db.upsert_fact(Fact(
        subject_type=CORRECTION,
        subject_id=thread_id,
        statement=statement.strip(),
        source="user",
        provenance="user",
    ))


def _has_user_written(person_id: str) -> bool:
    row = db.get_connection().execute(
        "SELECT 1 FROM messages WHERE person_id = ? AND is_from_user = 1 LIMIT 1",
        (person_id,),
    ).fetchone()
    return row is not None


# ------------------------------------------------------------------ misc
def _require(thread_id: str) -> Thread:
    thread = db.get_thread(thread_id)
    if not thread:
        raise ThreadError(f"no thread {thread_id}")
    return thread
