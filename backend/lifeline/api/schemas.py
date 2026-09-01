"""Wire format between the backend and the iOS client."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..models import Item

# --- v1.1 action helpers -------------------------------------------------
_URL_RE = re.compile(r'https?://[^\s<>"\')]+')
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "tiktok.com", "twitch.tv")


def _item_link(item: Item) -> Optional[str]:
    """The item's link — the extracted entity, else the first URL in the text."""
    if item.entities.link:
        return item.entities.link
    m = _URL_RE.search(item.raw_text or "")
    return m.group(0) if m else None


def _link_kind(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    lowered = url.lower()
    return "video" if any(h in lowered for h in _VIDEO_HOSTS) else "article"


def _counterpart_handle(item: Item) -> Optional[str]:
    """The other person's phone/email, for sms:/tel:. Person handle first, then
    the tail of an iMessage thread id (1:1 only; group ids have no handle)."""
    from .. import db
    if item.person_id:
        person = db.get_person(item.person_id)
        if person and person.handles:
            return person.handles[0]
    tail = (item.conversation_id or "").rsplit(";", 1)[-1]
    return tail if (tail.startswith("+") or "@" in tail) else None


class EntitiesOut(BaseModel):
    item: Optional[str] = None
    date: Optional[str] = None
    link: Optional[str] = None


class ExplanationOut(BaseModel):
    signal: str
    value: float
    weight: float
    contribution: float
    detail: str


class ItemOut(BaseModel):
    id: str
    source: str
    conversation_id: str
    person: str
    person_id: Optional[str] = None
    timestamp: str
    type: str
    raw_text: str
    entities: EntitiesOut
    suggested_action: str
    suggested_reply: Optional[str] = None
    status: str
    kind: str = "action"                    # action|information (§v1.4)
    category: Optional[str] = None          # information: discovery|context|external
    created_at: str
    updated_at: str
    # ranking
    score: float
    interruption_level: str
    why: List[ExplanationOut] = Field(default_factory=list)
    behavior_pattern: Optional[str] = None
    snoozed_until: Optional[str] = None
    completed_at: Optional[str] = None
    completed_by: Optional[str] = None
    links_to_item_id: Optional[str] = None
    # actions (v1.1)
    handle: Optional[str] = None
    link: Optional[str] = None
    link_kind: Optional[str] = None

    @classmethod
    def of(cls, item: Item) -> "ItemOut":
        link = _item_link(item)
        return cls(
            handle=_counterpart_handle(item),
            link=link,
            link_kind=_link_kind(link),
            id=item.id,
            source=item.source,
            conversation_id=item.conversation_id,
            person=item.person,
            person_id=item.person_id,
            timestamp=item.timestamp,
            type=item.type,
            raw_text=item.raw_text,
            entities=EntitiesOut(**{"item": item.entities.item, "date": item.entities.date, "link": item.entities.link}),
            suggested_action=item.suggested_action,
            suggested_reply=item.suggested_reply,
            status=item.status,
            kind=item.kind,
            category=item.category,
            created_at=item.created_at,
            updated_at=item.updated_at,
            score=item.score,
            interruption_level=item.interruption_level,
            why=[ExplanationOut(**e) for e in item.score_explanation],
            behavior_pattern=item.behavior_pattern,
            snoozed_until=item.snoozed_until,
            completed_at=item.completed_at,
            completed_by=item.completed_by,
            links_to_item_id=item.links_to_item_id,
        )


# --- v2 threads ----------------------------------------------------------
class EvidenceOut(BaseModel):
    """One row a thread has claimed, resolved for reading. This *is* the
    receipt — the trace is a product feature, not debug output."""
    kind: str                              # item | message | calendar_event
    ref_id: str
    title: str
    text: str = ""
    person: Optional[str] = None
    person_id: Optional[str] = None
    timestamp: Optional[str] = None
    date: Optional[str] = None
    status: Optional[str] = None
    source: Optional[str] = None
    role: str = "claimed"                  # claimed | founding
    note: Optional[str] = None
    linked_at: Optional[str] = None


class DeadlineOut(BaseModel):
    """A deadline chip and where it came from. `source=inferred` always carries
    evidence; the user's own date needs none, because the user is the source."""
    date: Optional[str] = None
    source: Optional[str] = None           # inferred | user
    reason: Optional[str] = None
    evidence: List[EvidenceOut] = Field(default_factory=list)


class ActivityMarkOut(BaseModel):
    """One mark on a lane's activity track — the system's work, over time.
    `kind` is `evidence` today; `finding` and `action` light up in step 4."""
    at: str
    kind: str


def _contact_name(person_id: Optional[str]) -> Optional[str]:
    """Resolved here so every lane and detail carries a name the client can
    render without a second round trip for a single string."""
    if not person_id:
        return None
    from .. import db

    person = db.get_person(person_id)
    return person.display_name if person else None


class ThreadOut(BaseModel):
    """§v2 — an open loop the user is carrying.

    Carries everything a lane renders. The client never re-decides urgency —
    `lane` is computed server-side, the same rule `Theme.swift` already states
    for interruption levels.
    """
    id: str
    title: str
    summary: str = ""
    origin: str                            # user|promoted-from-item|system-proposed|silence|urgency
    state: str                             # proposed|live|quiet|resolved|archived
    importance: float = 0.5
    deadline: Optional[DeadlineOut] = None
    evidence_count: int = 0
    opened_at: str
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None      # user | evidence
    updated_at: str
    # --- lane presentation (§v2 step 2) ---
    autonomy: str = "prepared"             # silent|prepared|ask — the ladder ceiling
    contact_person_id: Optional[str] = None
    contact_name: Optional[str] = None     # resolved for display
    # §v2.1 — where this sits on the front page. Decided server-side for the
    # same reason `lane` is: the client lacks the evidence to re-judge it.
    tier: str = "index"                    # lead|brief|index|quiet|closed
    pressure: float = 0.0                  # 0-1, what today demands of this
    lane: str = "live"                     # hot|warm|live|idle|done — the stripe
    subtitle: Optional[str] = None         # the one line under the title
    unseen: int = 0                        # the "N NEW" badge
    activity: List[ActivityMarkOut] = Field(default_factory=list)
    # §v2.7 — what the system has done with this thread, decided here for the
    # same reason `lane` is. `status_label` travels with it so the phone never
    # has to own a second copy of the wording.
    status: str = "none"                   # overdue|needs_you|queued|watching|finished|none
    status_label: Optional[str] = None
    # §v3 (Loose Ends) — the reason chip: why the card sits where it does, in
    # words. One per card, decided here for the same reason `tier` is; the
    # client maps `why_kind` to a tone and prints `why_text` verbatim.
    why_kind: Optional[str] = None         # overdue|due|move|new|waited|tied
    why_text: Optional[str] = None         # "overdue", "due Wed", "move ready", ...

    @classmethod
    def of(cls, thread: Any, evidence_count: int = 0) -> "ThreadOut":
        return cls(
            id=thread.id,
            title=thread.title,
            summary=thread.summary,
            origin=thread.origin,
            state=thread.state,
            importance=thread.importance,
            deadline=(
                DeadlineOut(
                    date=thread.deadline,
                    source=thread.deadline_source,
                    reason=thread.deadline_reason,
                )
                if thread.deadline
                else None
            ),
            evidence_count=evidence_count,
            opened_at=thread.opened_at,
            resolved_at=thread.resolved_at,
            resolved_by=thread.resolved_by,
            updated_at=thread.updated_at,
            autonomy=getattr(thread, "autonomy", "prepared"),
            contact_person_id=getattr(thread, "contact_person_id", None),
            contact_name=_contact_name(getattr(thread, "contact_person_id", None)),
        )


class ThreadStackOut(BaseModel):
    """The main view. `running` / `needs_you` are the header line; `threads`
    is the stack itself, with resolved ones riding along for a day so the user
    can *see* the pile shrink."""
    generated_at: str
    running: int
    needs_you: int
    threads: List[ThreadOut] = Field(default_factory=list)


class FindingOut(BaseModel):
    """What the worker brought back. `kind='nothing'` is a real result — the
    system looked and found nothing, and hiding that would misrepresent its
    work. `loop_run_id` is always set, so the receipts can be opened."""
    id: str
    kind: str                              # finding | action | nothing
    headline: str
    body: str = ""
    importance: float = 0.5
    created_at: str
    loop_run_id: Optional[str] = None
    evidence: List[EvidenceOut] = Field(default_factory=list)
    # --- §v2.1, a move (kind == action) ---
    move_kind: Optional[str] = None        # send | decide | gather | do
    steps: List[str] = Field(default_factory=list)
    needs: List[str] = Field(default_factory=list)
    # Named but not stageable. The client renders this differently from a move
    # that's ready — "here's what needs doing and why I couldn't do it" is a
    # different offer from "here it is, one tap".
    blocked_reason: Optional[str] = None
    # --- §v2.3 ---
    # False while this is the thread's current picture, True once a newer
    # finding of the same kind replaced it. The client leads with what is
    # current and collapses the rest into history.
    superseded: bool = False
    # Verified figures with their sources — legal on a finding, not just a
    # move, which is the whole point of them.
    facts: List[Dict[str, str]] = Field(default_factory=list)


class WatcherOut(BaseModel):
    """A standing monitor the thread implied. `times_fired` is the honest part:
    a watcher that has never fired is still doing its job, and saying so beats
    implying activity that didn't happen."""
    id: str
    kind: str                              # mail | messages | calendar | deadline
    what: str
    every_minutes: int
    until: Optional[str] = None
    times_fired: int = 0
    last_fired_at: Optional[str] = None


class CorrectionOut(BaseModel):
    """One thing the user told this thread after the system got it wrong."""
    id: str
    statement: str
    said_at: str


class ThreadDetailOut(BaseModel):
    thread: ThreadOut
    evidence: List[EvidenceOut] = Field(default_factory=list)
    findings: List[FindingOut] = Field(default_factory=list)
    watchers: List[WatcherOut] = Field(default_factory=list)
    corrections: List[CorrectionOut] = Field(default_factory=list)


class RejectIn(BaseModel):
    """Why a move was turned down — the whole point of v2.4's reject sheet.

    One tap used to mean three different things, and all three narrowed what
    the worker could offer. Naming the reason is what lets "you misread this"
    stop costing the user a capability they wanted to keep.
    """
    # handled  — the user did it themselves; teaches nothing
    # wrong    — the system misread the job; records `text`, leaves appetite alone
    # unwanted — the pre-v2.4 behaviour: fewer moves of this shape here
    reason: str = "unwanted"
    text: Optional[str] = None


class CorrectionIn(BaseModel):
    statement: str


class ThreadClosureOut(BaseModel):
    """"Did this close?" — the ask band, with its argument spelled out.

    Named reasons rather than a score, because closing something on the user's
    behalf has to be defensible. Auto-closes never appear here; they already
    happened and show as a resolved thread on the stack.
    """
    id: str
    thread: ThreadOut
    confidence: float
    reasons: List[str] = Field(default_factory=list)
    evidence: List[EvidenceOut] = Field(default_factory=list)
    detected_at: str


class ThreadDraftOut(BaseModel):
    """A reply written for a thread at write-time, or a refusal.

    `draft` is null when the writer decided a message isn't the work — a bill
    gets paid, a booking gets checked, and neither is answered by writing to a
    no-reply address. `reason` then says what to do instead, which is a better
    answer than a draft nobody can send.
    """
    thread_id: str
    draft: Optional["DraftOut"] = None
    reason: str = ""
    trace: List["TraceStepOut"] = Field(default_factory=list)


class ThreadIn(BaseModel):
    """Declare a thread. A thread the user types is live by default — they just
    told you they're carrying it."""
    title: str
    summary: str = ""
    state: str = "live"                    # live | quiet | proposed
    importance: float = 0.5
    deadline: Optional[str] = None         # ISO-8601; user-set, so no evidence needed
    # Who to write to if this thread ever needs a message. A thread the user
    # declares has no evidence to infer it from.
    contact_person_id: Optional[str] = None


class PersonOut(BaseModel):
    """One candidate for a thread's contact."""
    id: str
    display_name: str
    handle: Optional[str] = None
    message_count: int = 0


class ContactIn(BaseModel):
    """`None` clears the contact — the thread stops having a counterpart."""
    person_id: Optional[str] = None


class ThreadEditIn(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    state: Optional[str] = None            # live | quiet | resolved | archived
    importance: Optional[float] = None


class AutonomyIn(BaseModel):
    """How far this thread may go on its own.

    `silent` reads only. `prepared` may also write something the user can
    review. `ask` is reserved for tools that spend or commit. There is no
    setting for "send as you" — that line does not move at any tier.
    """
    ceiling: str                           # silent | prepared | ask


class DeadlineIn(BaseModel):
    """Set or clear a deadline. `source=inferred` must name its evidence;
    `source=user` is the override and always wins."""
    date: Optional[str] = None
    source: str = "user"                   # user | inferred
    reason: Optional[str] = None
    evidence: List[Dict[str, str]] = Field(default_factory=list)  # [{kind, ref_id}]


class EvidenceIn(BaseModel):
    ref_id: str
    kind: str = "item"                     # item | message | calendar_event
    role: str = "claimed"
    note: Optional[str] = None


class PromoteIn(BaseModel):
    """Promote a surfaced item into a loop the user is carrying. The item is
    not consumed — it becomes the thread's founding evidence."""
    title: Optional[str] = None
    summary: Optional[str] = None


class GroupOut(BaseModel):
    """A rendered group. `style` is the adaptive level-of-detail hint (§8.1)."""

    level: str
    title: str
    subtitle: Optional[str] = None
    style: str                    # expanded | compact | collapsed
    items: List[ItemOut]


class TodayOut(BaseModel):
    mode: str                     # empty | surge | briefing | day | evening
    headline: str
    subhead: Optional[str] = None
    generated_at: str
    groups: List[GroupOut]
    confirmations: List["ConfirmationOut"] = Field(default_factory=list)
    # `confirmations` is the top slice, not the whole queue — see
    # FEED_CONFIRMATIONS. This is how many there actually are, so a client can
    # say "50 of 11,569" rather than implying it is showing everything.
    confirmations_total: int = 0
    counts: Dict[str, int] = Field(default_factory=dict)
    # v1.1 — one-tap items for the quick-action carousel (a lens: these also
    # appear in `groups`).
    carousel: List[ItemOut] = Field(default_factory=list)


def is_quick_action(item: Item) -> bool:
    """One low-friction tap: a ready draft to send, or an openable link."""
    has_reply = bool(item.suggested_reply) and bool(_counterpart_handle(item))
    return has_reply or bool(_item_link(item))


class ConversationSummaryOut(BaseModel):
    person_id: Optional[str]
    person: str
    relationship: Optional[str] = None
    sources: List[str]
    open_count: int
    top_level: str
    last_activity: Optional[str] = None
    topic: Optional[str] = None
    tie_strength: float = 0.0        # 0..1 — drives the Life Threads strand weight


class HistoryEntryOut(BaseModel):
    item: ItemOut
    closed_by: str                # auto | manual
    closed_at: Optional[str]
    evidence: Optional[str] = None
    evidence_source: Optional[str] = None


class HistoryOut(BaseModel):
    entries: List[HistoryEntryOut]
    auto_closed: int
    manual_closed: int
    streak_days: int


class ConfirmationOut(BaseModel):
    signal_id: str
    item: ItemOut
    source: str
    confidence: float
    evidence_text: str
    reasons: List[str]
    detected_at: str


class MessageOut(BaseModel):
    """One line of the surrounding conversation, for the in-card context strip."""
    id: str
    sender: str                   # "You" or the counterpart's display name
    is_from_user: bool
    timestamp: str
    text: str
    is_pivot: bool = False        # the message the surfaced item was drawn from


class ConversationContextOut(BaseModel):
    """A short window of the thread around a surfaced item — the memory-jog."""
    item_id: str
    conversation_id: str
    person: str
    messages: List[MessageOut]


class DossierOut(BaseModel):
    """The receipts behind a surfaced item — why it's here + the evidence + where
    you left off. The universal 'so I understand what I'm looking at' payload."""
    why: List[str] = Field(default_factory=list)          # plain-language reasons it surfaced
    your_last_word: Optional[MessageOut] = None            # your last message in the thread
    awaiting_reply: bool = False                           # you spoke last; no answer yet
    messages: List[MessageOut] = Field(default_factory=list)  # the surrounding thread (evidence)


class SnoozeIn(BaseModel):
    hours: Optional[float] = None
    until: Optional[str] = None


class PairClaimIn(BaseModel):
    """§v3 — a device spending a pairing code for its bearer token."""
    code: str
    device_name: str = ""


class WaitingPersonOut(BaseModel):
    """A person you owe, for the 'who's waiting on you' proactive surface."""
    person_id: Optional[str]
    person: str
    tie_strength: float                # 0..1 — how close the model thinks you are
    tie_label: str                     # human read of the tie
    waited_since: str                  # ISO of the oldest open item
    open_count: int
    top_item: ItemOut                  # the most important open item from them


class BriefingOut(BaseModel):
    """The proactive read: the one thing to do now + who's waiting on you.
    The server decides what's briefing-worthy; clients and the notifier render it."""
    generated_at: str
    mode: str                          # morning | day | evening
    caught_up: bool
    one_now: Optional[ItemOut] = None  # the single highest-value thing right now
    waiting: List[WaitingPersonOut] = Field(default_factory=list)


class CalendarEventIn(BaseModel):
    """A device-calendar event pushed from the phone (EventKit)."""
    id: str
    calendar_id: str = "device"
    summary: str
    description: str = ""
    location: str = ""
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    self_response: Optional[str] = None


class CalendarSyncIn(BaseModel):
    events: List[CalendarEventIn]


class AskIn(BaseModel):
    """A question for the assistant."""
    question: str


class ReceiptOut(BaseModel):
    """One source an answer rests on — tappable, openable, checkable."""
    kind: str                                     # message | fact
    ref_id: str
    source: str = ""                              # imessage | gmail | world
    label: str
    detail: str = ""


class KnownFactOut(BaseModel):
    """A fact the world model contributed to an answer, correctable by id."""
    fact_id: str
    entity_id: str
    entity: str
    predicate: str
    value: str


class AskOut(BaseModel):
    """§v2.9 — an answer card: the answer, its receipts, what was already
    known, and the trace. Not a chat turn."""
    id: str = ""
    question: str = ""
    answer: str
    sources: List[str] = Field(default_factory=list)     # tool names (legacy)
    receipts: List[ReceiptOut] = Field(default_factory=list)
    knew: List[KnownFactOut] = Field(default_factory=list)
    trace: List["TraceStepOut"] = Field(default_factory=list)
    created_at: str = ""


class TellIn(BaseModel):
    """First-person knowledge, in the user's words (§v1.4 pillar B)."""
    text: str


class FactOut(BaseModel):
    id: str
    subject_type: str
    subject_id: Optional[str] = None
    statement: str
    predicate: Optional[str] = None
    value: Optional[str] = None
    source: str
    confidence: float
    status: str
    updated_at: str


class TellOut(BaseModel):
    reply: str                                    # what the assistant captured, in words
    facts: List[FactOut] = Field(default_factory=list)  # the facts it recorded


class TraceStepOut(BaseModel):
    """One tool call from the loop, shown to the user as a receipt — the
    sanity trail behind an answer. Misses are honest, never silent."""
    tool: str
    summary: str                                  # what it looked for / found
    ok: bool = True                               # False → error or nothing found


class ConverseIn(BaseModel):
    """One line to the aide — a statement, a question, or both. Pass the
    session_id from a previous reply to continue that conversation; omit it to
    start a new one."""
    text: str
    session_id: Optional[str] = None


class DraftOut(BaseModel):
    """A message the aide wrote for the user to review and send. The app never
    sends on its own — one tap opens Messages/Mail with this prefilled."""
    person: str
    person_id: Optional[str] = None
    handle: Optional[str] = None
    channel: str = "imessage"                            # imessage|email
    text: str


class ConverseOut(BaseModel):
    reply: str
    session_id: str                                      # carry this into the next turn
    facts: List[FactOut] = Field(default_factory=list)   # recorded, if it was a tell
    draft: Optional[DraftOut] = None                     # ready to send, if asked
    trace: List[TraceStepOut] = Field(default_factory=list)  # tools fired, in order


class TurnOut(BaseModel):
    """One persisted turn of a conversation."""
    id: str
    role: str                                            # user|assistant
    text: str
    facts: List[FactOut] = Field(default_factory=list)
    trace: List[TraceStepOut] = Field(default_factory=list)
    created_at: str


class ConversationOut(BaseModel):
    session_id: str
    turns: List[TurnOut] = Field(default_factory=list)


class FactEditIn(BaseModel):
    """A user's correction to the model of you — the highest-quality label."""
    statement: Optional[str] = None
    predicate: Optional[str] = None
    value: Optional[str] = None
    status: Optional[str] = None                  # active|dismissed


class PersonFactsOut(BaseModel):
    person_id: Optional[str] = None
    name: str
    tie_label: Optional[str] = None
    facts: List[FactOut] = Field(default_factory=list)


class ModelOut(BaseModel):
    """The model of you (§v1.4): everything the system believes, grouped —
    inspectable and editable, the user overrules the system."""
    you: List[FactOut] = Field(default_factory=list)
    people: List[PersonFactsOut] = Field(default_factory=list)
    topics: List[PersonFactsOut] = Field(default_factory=list)  # name = topic slug


class BatchIn(BaseModel):
    """The items the user staged to clear together (Phase D)."""
    item_ids: List[str]


class DraftBatchOut(BaseModel):
    """One reply that answers several owed items to one person."""
    reply: str
    handle: Optional[str] = None       # sms/tel target for the Send deep link
    item_ids: List[str]                # the items this reply actually covers


class BatchDoneOut(BaseModel):
    completed: List[str]               # ids actually closed


class DeviceIn(BaseModel):
    token: str
    platform: str = "ios"


class ImportIn(BaseModel):
    source: str                   # imessage | whatsapp
    path: str
    contact_name: Optional[str] = None
    is_group: bool = False


class ActionOut(BaseModel):
    ok: bool = True
    item: Optional[ItemOut] = None
    detail: Optional[str] = None


TodayOut.model_rebuild()
ThreadDraftOut.model_rebuild()
AskOut.model_rebuild()
