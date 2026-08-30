"""Typed representations of the Section 5 schema and its neighbours."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ITEM_TYPES = ("purchase", "event", "promise", "followup", "reading", "question")
STATUSES = ("pending", "completed", "snoozed", "dismissed")
SOURCES = ("imessage", "whatsapp", "gmail")


class InterruptionLevel:
    """§6.1 — modelled on Apple's notification framework."""

    TIME_SENSITIVE = "time_sensitive"
    ACTIVE = "active"
    PASSIVE = "passive"

    ALL = (TIME_SENSITIVE, ACTIVE, PASSIVE)
    ORDER = {TIME_SENSITIVE: 0, ACTIVE: 1, PASSIVE: 2}


class BehaviorPattern:
    """§6.3 — the two readings of an un-actioned item."""

    AVOIDANCE = "avoidance"
    DEPRIORITIZED = "deprioritized"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Person:
    id: str
    display_name: str
    relationship: Optional[str] = None
    handles: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)


@dataclass
class Conversation:
    id: str
    source: str
    display_name: str
    is_group: bool = False
    created_at: str = field(default_factory=now_iso)


@dataclass
class Message:
    id: str
    source: str
    conversation_id: str
    external_id: str
    timestamp: str
    text: str
    person_id: Optional[str] = None
    is_from_user: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    extracted_at: Optional[str] = None
    ingested_at: str = field(default_factory=now_iso)


@dataclass
class CalendarEvent:
    id: str
    calendar_id: str
    summary: str
    description: str = ""
    location: str = ""
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    status: str = "confirmed"
    attendees: List[str] = field(default_factory=list)
    self_response: Optional[str] = None
    updated_at: str = field(default_factory=now_iso)
    ingested_at: str = field(default_factory=now_iso)


@dataclass
class Attachment:
    """A file a message carried (§v2.8 phase 0). `text` is what a parser got
    out of it — the part of the message the body walk always dropped."""

    id: str = field(default_factory=new_id)
    message_id: str = ""
    source: str = "gmail"
    remote_id: Optional[str] = None
    filename: str = ""
    mime: str = ""
    size_bytes: int = 0
    sha256: str = ""
    text: Optional[str] = None
    parsed_at: Optional[str] = None
    error: Optional[str] = None
    ingested_at: str = field(default_factory=now_iso)


@dataclass
class Entities:
    item: Optional[str] = None
    date: Optional[str] = None
    link: Optional[str] = None


@dataclass
class Item:
    """The Section 5 output schema."""

    id: str = field(default_factory=new_id)
    source: str = "imessage"
    conversation_id: str = ""
    person: str = ""
    timestamp: str = field(default_factory=now_iso)
    type: str = "question"
    raw_text: str = ""
    entities: Entities = field(default_factory=Entities)
    suggested_action: str = ""
    suggested_reply: Optional[str] = None
    status: str = "pending"
    # §v1.4 surfacing axis: an action you owe, or information worth knowing.
    kind: str = "action"                # action|information
    category: Optional[str] = None      # information: discovery|context|external
    created_at: str = field(default_factory=now_iso)

    # engine-owned fields
    message_id: Optional[str] = None
    person_id: Optional[str] = None
    score: float = 0.0
    interruption_level: str = InterruptionLevel.ACTIVE
    score_explanation: List[Dict[str, Any]] = field(default_factory=list)
    behavior_pattern: Optional[str] = None
    snoozed_until: Optional[str] = None
    completed_at: Optional[str] = None
    completed_by: Optional[str] = None
    links_to_item_id: Optional[str] = None
    updated_at: str = field(default_factory=now_iso)

    def to_spec_dict(self) -> Dict[str, Any]:
        """Exactly the JSON shape documented in Section 5."""
        return {
            "id": self.id,
            "source": self.source,
            "conversation_id": self.conversation_id,
            "person": self.person,
            "timestamp": self.timestamp,
            "type": self.type,
            "raw_text": self.raw_text,
            "entities": asdict(self.entities),
            "suggested_action": self.suggested_action,
            "suggested_reply": self.suggested_reply,
            "status": self.status,
            "created_at": self.created_at,
        }

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "person_id": self.person_id,
            "person": self.person,
            "timestamp": self.timestamp,
            "type": self.type,
            "raw_text": self.raw_text,
            "entity_item": self.entities.item,
            "entity_date": self.entities.date,
            "entity_link": self.entities.link,
            "suggested_action": self.suggested_action,
            "suggested_reply": self.suggested_reply,
            "status": self.status,
            "kind": self.kind,
            "category": self.category,
            "score": self.score,
            "interruption_level": self.interruption_level,
            "score_explanation": json.dumps(self.score_explanation),
            "behavior_pattern": self.behavior_pattern,
            "snoozed_until": self.snoozed_until,
            "completed_at": self.completed_at,
            "completed_by": self.completed_by,
            "links_to_item_id": self.links_to_item_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Item":
        d = dict(row)
        return cls(
            id=d["id"],
            source=d["source"],
            conversation_id=d["conversation_id"],
            message_id=d.get("message_id"),
            person_id=d.get("person_id"),
            person=d["person"],
            timestamp=d["timestamp"],
            type=d["type"],
            kind=d.get("kind") or "action",
            category=d.get("category"),
            raw_text=d["raw_text"],
            entities=Entities(item=d.get("entity_item"), date=d.get("entity_date"), link=d.get("entity_link")),
            suggested_action=d.get("suggested_action") or "",
            suggested_reply=d.get("suggested_reply"),
            status=d.get("status") or "pending",
            score=d.get("score") or 0.0,
            interruption_level=d.get("interruption_level") or InterruptionLevel.ACTIVE,
            score_explanation=json.loads(d.get("score_explanation") or "[]"),
            behavior_pattern=d.get("behavior_pattern"),
            snoozed_until=d.get("snoozed_until"),
            completed_at=d.get("completed_at"),
            completed_by=d.get("completed_by"),
            links_to_item_id=d.get("links_to_item_id"),
            created_at=d.get("created_at") or now_iso(),
            updated_at=d.get("updated_at") or now_iso(),
        )


class ThreadState:
    """§v2 — where a thread sits in the lifecycle.

    `PROPOSED` is deliberately outside the main stack: the system may propose
    loops, but only the user's acceptance puts one on the pile. That is what
    keeps the thread count meaningful.
    """

    PROPOSED = "proposed"
    LIVE = "live"
    QUIET = "quiet"
    RESOLVED = "resolved"
    ARCHIVED = "archived"

    ALL = (PROPOSED, LIVE, QUIET, RESOLVED, ARCHIVED)
    OPEN = (LIVE, QUIET)          # the main stack — what the user is carrying
    CLOSED = (RESOLVED, ARCHIVED)


class ThreadOrigin:
    """Who declared the loop. Not the same axis as state: `SYSTEM_PROPOSED`
    always pairs with state=proposed, while `SILENCE` and `URGENCY` open a live
    thread directly — the world declared it, so the user already has it."""

    USER = "user"
    PROMOTED = "promoted-from-item"
    SYSTEM_PROPOSED = "system-proposed"
    SILENCE = "silence"            # §v2: the silence sweep, step 1
    URGENCY = "urgency"            # §v2: the deadline-inside-24h bypass, step 4

    ALL = (USER, PROMOTED, SYSTEM_PROPOSED, SILENCE, URGENCY)


class DeadlineSource:
    INFERRED = "inferred"          # read off evidence; shows its receipts
    USER = "user"                  # the user overruled, and the user always wins

    ALL = (INFERRED, USER)


EVIDENCE_KINDS = ("item", "message", "calendar_event")


@dataclass
class Evidence:
    """One row a thread has claimed. Polymorphic because the thread's founding
    fact is not always an item: the silence sweep's is a message, and an
    inferred deadline can come off a calendar event."""

    thread_id: str = ""
    kind: str = "item"                  # item|message|calendar_event
    ref_id: str = ""
    role: str = "claimed"               # claimed|founding
    note: Optional[str] = None
    linked_at: str = field(default_factory=now_iso)

    def ref(self) -> Dict[str, str]:
        """The compact {kind, ref_id} form deadline provenance is stored in."""
        return {"kind": self.kind, "ref_id": self.ref_id}


@dataclass
class Thread:
    """§v2 — an open loop in the user's head. The unit of the whole product.

    Note what is *not* here: no person_id, no type. A thread is a goal, not a
    message about someone, and the audit's finding that the learning key space
    has nothing to key on is real — it gets designed in step 7b rather than
    pre-empted with a column that would be wrong half the time. Who a thread
    concerns is reachable through its evidence.
    """

    id: str = field(default_factory=new_id)
    title: str = ""
    summary: str = ""
    origin: str = ThreadOrigin.USER
    state: str = ThreadState.LIVE
    # A stable dedupe key for system-produced threads ("silence:<conv>:<ts>"),
    # so a producer that runs every poll cycle can't pile up duplicates.
    # NULL for user-declared threads, which are never deduped.
    key: Optional[str] = None
    deadline: Optional[str] = None
    deadline_source: Optional[str] = None       # inferred|user
    deadline_reason: Optional[str] = None       # "the bill says due Aug 31"
    # Which evidence implied the deadline: [{"kind": ..., "ref_id": ...}].
    # Receipts are a product feature — an inferred date that can't show its
    # source is just a guess with better typography.
    deadline_evidence: List[Dict[str, str]] = field(default_factory=list)
    importance: float = 0.5
    opened_at: str = field(default_factory=now_iso)
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None           # user|evidence
    # When the user last opened this thread. Everything that arrived after it
    # is what the lane's "N NEW" badge counts.
    last_seen_at: Optional[str] = None
    # When the worker loop last ran this thread, and how far it may go.
    last_worked_at: Optional[str] = None
    autonomy: str = "prepared"
    # Who to write to when this thread needs a message. Not "who the
    # thread is about" — see the class docstring; this is the counterpart,
    # and most threads have none.
    contact_person_id: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "origin": self.origin,
            "state": self.state,
            "key": self.key,
            "deadline": self.deadline,
            "deadline_source": self.deadline_source,
            "deadline_reason": self.deadline_reason,
            "deadline_evidence": json.dumps(self.deadline_evidence),
            "importance": self.importance,
            "opened_at": self.opened_at,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
            "last_seen_at": self.last_seen_at,
            "last_worked_at": self.last_worked_at,
            "autonomy": self.autonomy,
            "contact_person_id": self.contact_person_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Thread":
        d = dict(row)
        return cls(
            id=d["id"],
            title=d["title"],
            summary=d.get("summary") or "",
            origin=d.get("origin") or ThreadOrigin.USER,
            state=d.get("state") or ThreadState.LIVE,
            key=d.get("key"),
            deadline=d.get("deadline"),
            deadline_source=d.get("deadline_source"),
            deadline_reason=d.get("deadline_reason"),
            deadline_evidence=json.loads(d.get("deadline_evidence") or "[]"),
            importance=d.get("importance") if d.get("importance") is not None else 0.5,
            opened_at=d.get("opened_at") or now_iso(),
            resolved_at=d.get("resolved_at"),
            resolved_by=d.get("resolved_by"),
            last_seen_at=d.get("last_seen_at"),
            last_worked_at=d.get("last_worked_at"),
            autonomy=d.get("autonomy") or "prepared",
            contact_person_id=d.get("contact_person_id"),
            created_at=d.get("created_at") or now_iso(),
            updated_at=d.get("updated_at") or now_iso(),
        )


class Autonomy:
    """§v2 — the ladder, per thread. The ceiling is **user-set, never learned**:
    `PREPARED` needs no permission already, so the only tier learning could
    promote *into* is `ASK` (spends money, hard to undo), which would turn
    unrelated draft approvals into consent for irreversible acts. Learning may
    move a thread down, never up."""

    SILENT = "silent"        # research, watch, gather, summarise — it's reading
    PREPARED = "prepared"    # may also draft and stage; visible, never sent
    ASK = "ask"              # may propose things that cost or commit

    ALL = (SILENT, PREPARED, ASK)
    ORDER = {SILENT: 0, PREPARED: 1, ASK: 2}


class FindingKind:
    FINDING = "finding"      # something worth knowing
    ACTION = "action"        # a move — something prepared for you
    NOTHING = "nothing"      # looked, found nothing — a real result

    ALL = (FINDING, ACTION, NOTHING)


class MoveKind:
    """§v2.1 — the four shapes a move takes, derived from the live threads.

    `DO` is the largest and least glamorous, and the one the design turns on.
    Most threads cannot be finished inside this app: a bill is paid on someone
    else's website, documents are uploaded to a county office, a decision is
    made by a human being. For those, initiative is not acting — it is
    assembling every number, date, link and document so the only work left is
    the part that genuinely needs the user.
    """

    SEND = "send"        # a message is the move; the draft is the work
    DECIDE = "decide"    # blocked on a choice; the options are the work
    GATHER = "gather"    # no action exists; the material is the value
    DO = "do"            # the user must act elsewhere; staging is the work

    ALL = (SEND, DECIDE, GATHER, DO)

    # Spending or committing is what `ask` was reserved for. A `do` move is the
    # only shape that can reach outside and cost something, so it is the only
    # one the ladder gates. See `registry.scoped_for`.
    NEEDS_ASK = (DO,)

    @staticmethod
    def allowed_for(ceiling: str) -> tuple:
        """Which shapes a thread at this ceiling may have proposed to it.

        `silent` gets none: at that rung the worker may say what is missing but
        never stage the thing that fills it — that is the whole content of the
        rung, and it is what the iOS copy promises ("Never writes a message").

        `prepared` gets everything reversible and free. `ask` additionally gets
        `do`, which is the shape that reaches outside the app and can cost
        money or be hard to undo.
        """
        if ceiling == Autonomy.SILENT:
            return ()
        if ceiling == Autonomy.ASK:
            return MoveKind.ALL
        return tuple(k for k in MoveKind.ALL if k not in MoveKind.NEEDS_ASK)


@dataclass
class Finding:
    """What the worker brings back, attached to the thread that wanted it.

    Always carries `loop_run_id`. The receipts are a product feature, and a
    finding whose provenance can't be opened is just an assertion.
    """

    id: str = field(default_factory=new_id)
    thread_id: str = ""
    kind: str = FindingKind.FINDING
    headline: str = ""
    body: str = ""
    importance: float = 0.5
    evidence: List[Dict[str, str]] = field(default_factory=list)
    # --- §v2.1, set only on a move (kind == ACTION) ---
    move_kind: Optional[str] = None
    # The staged work itself: the draft, the figures, the link, the options.
    steps: List[Dict[str, str]] = field(default_factory=list)
    # What only the user can supply — a signature, a card, a decision.
    needs: List[str] = field(default_factory=list)
    # Set when the move is named but could not be staged. A move the worker can
    # name but not prepare is still worth surfacing, and has to look different
    # from one that is ready.
    blocked_reason: Optional[str] = None
    # --- §v2.3, legal on ANY kind ---
    # Verified figures with their sources: [{label, value, url}]. The reason
    # this is not part of `steps` is that `steps` is staged work and belongs to
    # a move, so a pass that researched hard and concluded "no move yet" had
    # nowhere to put what it learned except prose. On the first full day of web
    # search, thirteen passes produced zero links: the model's only choices
    # were make-a-move or write-an-essay, and it was told to be conservative
    # about moves. This is the third option — report, with structure.
    facts: List[Dict[str, str]] = field(default_factory=list)
    loop_run_id: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    surfaced_at: Optional[str] = None
    dismissed_at: Optional[str] = None
    # §v2.3 — set when a newer finding of the same kind replaced this one.
    # Superseded, never deleted: the thread's history is the receipt that the
    # system was working, and a finding that vanishes takes that with it.
    superseded_at: Optional[str] = None

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "kind": self.kind,
            "headline": self.headline,
            "body": self.body,
            "importance": self.importance,
            "evidence": json.dumps(self.evidence),
            "move_kind": self.move_kind,
            "steps": json.dumps(self.steps),
            "needs": json.dumps(self.needs),
            "blocked_reason": self.blocked_reason,
            "facts": json.dumps(self.facts),
            "loop_run_id": self.loop_run_id,
            "created_at": self.created_at,
            "surfaced_at": self.surfaced_at,
            "dismissed_at": self.dismissed_at,
            "superseded_at": self.superseded_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Finding":
        d = dict(row)
        return cls(
            id=d["id"],
            thread_id=d["thread_id"],
            kind=d.get("kind") or FindingKind.FINDING,
            headline=d["headline"],
            body=d.get("body") or "",
            importance=d.get("importance") if d.get("importance") is not None else 0.5,
            evidence=json.loads(d.get("evidence") or "[]"),
            move_kind=d.get("move_kind"),
            steps=json.loads(d.get("steps") or "[]"),
            needs=json.loads(d.get("needs") or "[]"),
            blocked_reason=d.get("blocked_reason"),
            facts=json.loads(d.get("facts") or "[]"),
            loop_run_id=d.get("loop_run_id"),
            created_at=d.get("created_at") or now_iso(),
            surfaced_at=d.get("surfaced_at"),
            dismissed_at=d.get("dismissed_at"),
            superseded_at=d.get("superseded_at"),
        )


@dataclass
class CompletionSignal:
    id: str = field(default_factory=new_id)
    item_id: str = ""
    source: str = "gmail"
    evidence_ref: str = ""
    evidence_text: str = ""
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    resolution: str = "needs_confirmation"
    detected_at: str = field(default_factory=now_iso)
    resolved_at: Optional[str] = None


@dataclass
class Fact:
    """One piece of the model of you (§v1.4): a statement the user made
    (source=user) or the loop derived (source=derived). Soft-deleted via
    status so the user's corrections are themselves signal."""

    id: str = field(default_factory=new_id)
    # `thread` joined the set in v2.4, for a correction the user typed about
    # one loop: "it's for me to pick — don't ask her for size". It is a fact
    # rather than a table of its own because that is exactly what it is, and
    # because `status` already gives corrections the soft-delete the screen
    # needs — the user can take one back without the system forgetting it was
    # ever said.
    subject_type: str = "self"          # self|person|topic|thread
    subject_id: Optional[str] = None    # person_id / topic slug / thread_id; None for self
    statement: str = ""
    predicate: Optional[str] = None     # optional structured pair ...
    value: Optional[str] = None         # ... e.g. priority=low
    source: str = "user"                # user|derived
    confidence: float = 1.0
    provenance: Optional[str] = None    # loop_run id, or "user"
    status: str = "active"              # active|dismissed
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
