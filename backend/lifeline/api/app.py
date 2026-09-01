"""HTTP API consumed by the iOS client (§9).

The backend owns OAuth tokens, polling, extraction orchestration and the
learned model; the device keeps a local mirror and reports interactions back
so the learning loop has something to learn from.
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import __version__
from .. import db
from .. import threads as threads_mod
from ..threads import closure as thread_closure_engine
from ..threads import watchers as thread_watchers_mod
from ..completion import engine
from ..config import get_config
from ..assistant import enrich as assistant_enrich
from ..assistant import worker
from ..assistant import loop as assistant_loop
from ..assistant import registry as assistant_registry
from ..assistant import tools as assistant_tools
from ..extraction import batching, budget, pipeline, providers, topics
from ..extraction.dates import days_until
from ..extraction.claude import _parse_json
from ..ingestion import applemail, imessage, invites, whatsapp
from ..ingestion.base import IdentityResolver
from ..jobs import poller
from ..models import (
    CalendarEvent,
    Fact,
    InterruptionLevel,
    ThreadState,
    new_id,
    now_iso,
    parse_iso,
)
from ..notifications import scheduler
from ..ranking import learning, relationships, scorer
from . import presentation
from .schemas import (
    ActionOut,
    AskIn,
    AskOut,
    KnownFactOut,
    ReceiptOut,
    FactOut,
    BatchDoneOut,
    BatchIn,
    BriefingOut,
    CalendarSyncIn,
    ConfirmationOut,
    ConversationOut,
    ConverseIn,
    ConverseOut,
    ActivityMarkOut,
    AutonomyIn,
    ContactIn,
    PersonOut,
    DeadlineIn,
    DeadlineOut,
    DraftOut,
    EvidenceIn,
    EvidenceOut,
    FindingOut,
    WatcherOut,
    PromoteIn,
    ThreadClosureOut,
    CorrectionIn,
    CorrectionOut,
    RejectIn,
    ThreadDetailOut,
    ThreadDraftOut,
    ThreadEditIn,
    ThreadIn,
    ThreadOut,
    ThreadStackOut,
    TurnOut,
    TraceStepOut,
    FactEditIn,
    ModelOut,
    PersonFactsOut,
    TellIn,
    TellOut,
    DeviceIn,
    DossierOut,
    DraftBatchOut,
    HistoryEntryOut,
    HistoryOut,
    ImportIn,
    is_quick_action,
    ItemOut,
    MessageOut,
    PairClaimIn,
    SnoozeIn,
    ConversationContextOut,
    ConversationSummaryOut,
    TodayOut,
    WaitingPersonOut,
    _counterpart_handle,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.get_connection()      # migrate on boot
    from . import transport
    await asyncio.to_thread(transport.advertise)   # §v3 ws4 — never fatal
    task = asyncio.create_task(poller.run_forever())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        transport.withdraw()


app = FastAPI(title="Lifeline", version=__version__, lifespan=lifespan)

# §v3 — the gate. Every route below this line is reachable by exactly two
# kinds of caller: this Mac talking to itself, or a device that paired.
from . import auth as api_auth  # noqa: E402  (registered against `app`, so it lives here)

app.middleware("http")(api_auth.gate)


def _item_or_404(item_id: str):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, f"no item {item_id}")
    return item


def _confirmations() -> List[ConfirmationOut]:
    return [
        ConfirmationOut(
            signal_id=str(c["signal_id"]),
            item=ItemOut.of(c["item"]),          # type: ignore[arg-type]
            source=str(c["source"]),
            confidence=float(c["confidence"]),   # type: ignore[arg-type]
            evidence_text=str(c["evidence_text"]),
            reasons=list(c["reasons"]),          # type: ignore[arg-type]
            detected_at=str(c["detected_at"]),
        )
        for c in engine.open_confirmations()
    ]


# ----------------------------------------------------------------- health
@app.get("/doctor")
def doctor_report() -> Dict[str, Any]:
    """Every integration, exercised rather than inspected.

    `/health` reports configuration and answered `mail_readable: true`
    throughout the thirteen days Gmail was dead — the token row existed, it
    just did not work. This one refreshes the token, opens `chat.db` and makes
    a real (free) model call, so the answer is what would actually happen.

    Slower than `/health` by design; not for polling. The Mac app's setup flow
    is the intended caller.
    """
    from .. import doctor

    return doctor.run().as_dict()


@app.get("/health")
def health() -> Dict[str, Any]:
    cfg = get_config()
    return {
        "ok": True,
        "version": app.version,
        "mail_readable": applemail.available(),
        "claude_configured": cfg.has_claude,
        "gemini_configured": cfg.has_gemini,
        "apns_configured": cfg.has_apns,
        "open_items": len(db.open_items()),
        # A configured key that FAILS (dead credits, revoked) is worse than no
        # key — it degrades everything silently. Surface the last error, and
        # whether the engine is reduced to rules *right now*: extraction falls
        # back and carries on, so nothing else would say.
        "llm_last_error": db.get_sync_state("llm:last_error") or None,
        "degraded_since": db.get_sync_state("llm:degraded_since") or None,
        # Today's tokens per model, with a rough dollar estimate. Here because
        # "is it spending too much?" was previously only answerable from the
        # billing console, a day late — and the answer, when it finally came,
        # was that extraction on Opus cost 7x the entire agentic loop.
        "spend_today": budget.spend_report(),
    }


_DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


def _read_doc(name: str) -> str:
    from ..config import REPO_ROOT
    for base in (REPO_ROOT.parent, REPO_ROOT):
        p = base / name
        if p.exists():
            return p.read_text()
    return ""


from pydantic import BaseModel  # noqa: E402
import json as _json  # noqa: E402
import threading as _threading  # noqa: E402
import uuid as _uuid  # noqa: E402


class FeedbackIn(BaseModel):
    area: str = "Other"
    type: str = "Idea"
    priority: str = "med"
    note: str


def _feedback_file() -> Path:
    from ..config import REPO_ROOT
    return REPO_ROOT.parent / "docs" / "feedback.json"


_feedback_lock = _threading.Lock()


def _feedback_load() -> list:
    f = _feedback_file()
    if f.exists():
        try:
            return _json.loads(f.read_text())
        except Exception:
            return []
    return []


def _feedback_save(items: list) -> None:
    f = _feedback_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(_json.dumps(items, indent=2))


@app.get("/feedback")
def feedback_list() -> list:
    return _feedback_load()


@app.post("/feedback")
def feedback_add(entry: FeedbackIn) -> Dict[str, Any]:
    note = entry.note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="note is required")
    with _feedback_lock:
        items = _feedback_load()
        rec = {"id": _uuid.uuid4().hex[:12], "created_at": now_iso(),
               "area": entry.area, "type": entry.type, "priority": entry.priority, "note": note}
        items.insert(0, rec)
        _feedback_save(items)
    return rec


@app.delete("/feedback/{fid}")
def feedback_delete(fid: str) -> Dict[str, Any]:
    with _feedback_lock:
        _feedback_save([i for i in _feedback_load() if i.get("id") != fid])
    return {"ok": True}


@app.get("/people", response_model=List[PersonOut])
def list_people(q: Optional[str] = None, limit: int = 50) -> List[PersonOut]:
    """People the user actually talks to, for the thread-contact picker.

    Ordered by how much traffic there is with them rather than
    alphabetically: the contact you want is nearly always someone you speak
    to often, and an A-Z list of every handle that ever texted is a list of
    robots with a few humans hidden in it.
    """
    rows = db.get_connection().execute(
        "SELECT p.id, p.display_name, p.handles, COUNT(m.id) AS traffic "
        "FROM people p LEFT JOIN messages m ON m.person_id = p.id "
        "GROUP BY p.id ORDER BY traffic DESC"
    ).fetchall()

    needle = (q or "").strip().lower()
    out = []
    for r in rows:
        name = r["display_name"] or ""
        handles = _json.loads(r["handles"] or "[]")
        if needle and needle not in name.lower() and not any(
            needle in h.lower() for h in handles
        ):
            continue
        out.append(PersonOut(
            id=r["id"], display_name=name,
            handle=handles[0] if handles else None,
            message_count=r["traffic"] or 0,
        ))
        if len(out) >= limit:
            break
    return out


@app.get("/people/{person_id}/avatar")
def person_avatar(person_id: str):
    """The contact's photo, if we can read it from Contacts. 404 → monogram."""
    from fastapi import Response
    from ..ingestion import contacts
    person = db.get_person(person_id)
    if not person or not person.handles:
        raise HTTPException(404, "no photo")
    try:
        data = contacts.photo_for(person.handles)
    except Exception:
        data = None
    if not data:
        raise HTTPException(404, "no photo")
    return Response(content=data, media_type="image/jpeg")


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    """A project console: live status + the repo's VISION/PLAN + doc links."""
    import json as _json
    html = _DASHBOARD_HTML.read_text()
    html = html.replace("__VISION_JSON__", _json.dumps(_read_doc("VISION.md")))
    html = html.replace("__PLAN_JSON__", _json.dumps(_read_doc("PLAN.md")))
    return html


# ------------------------------------------------------------------ views
@app.get("/today", response_model=TodayOut)
def today(at: Optional[str] = None) -> TodayOut:
    """§8.2 Today — the ranked briefing, grouped and shaped for right now."""
    reference = parse_iso(at) or datetime.now(timezone.utc)
    items = scorer.ranked(reference)
    payload = presentation.build_today(items, reference)
    payload["confirmations"] = _confirmations()
    # A lens over the ranked items: the one-tap wins, for the quick carousel.
    payload["carousel"] = [ItemOut.of(i) for i in items if is_quick_action(i)][:6]
    return TodayOut(**payload)     # type: ignore[arg-type]


@app.get("/queue", response_model=List[ItemOut])
def queue(at: Optional[str] = None, include_snoozed: bool = False) -> List[ItemOut]:
    """The triage deck — every open item, highest priority first, one card at a
    time. Snoozed items are held out until they wake; pass include_snoozed=true
    to get them too (the deck's stack rebuilds itself from these on launch)."""
    reference = parse_iso(at) or datetime.now(timezone.utc)
    return [ItemOut.of(i) for i in scorer.ranked(reference, include_snoozed=include_snoozed)]


@app.get("/briefing", response_model=BriefingOut)
def briefing(at: Optional[str] = None) -> BriefingOut:
    """The proactive read — the spine of one-thing-now, who's-waiting, and the
    notification scheduler. The server decides what's worth surfacing; clients
    and the notifier just render it (§8.1)."""
    reference = parse_iso(at) or datetime.now(timezone.utc)
    items = scorer.ranked(reference)                       # pending, ranked, awake
    hour = reference.astimezone().hour
    mode = "morning" if 6 <= hour < 11 else "evening" if hour >= 18 else "day"

    # Both surfaces are about things someone is waiting on you for — a decision or
    # a reply — not "when you get to it" reading you do for yourself. Passive and
    # reading items are excluded so a pile of stale links doesn't read as "16
    # people waiting."
    actionable = [
        i for i in items
        if i.interruption_level != InterruptionLevel.PASSIVE and i.type != "reading"
    ]

    def _due_days(item) -> float:
        d = days_until(item.entities.date, reference)
        return d if d is not None else 9_999.0

    # Who's waiting — group by person, rank by tie strength then wait time.
    by_person: Dict[Optional[str], List] = {}
    for item in actionable:
        by_person.setdefault(item.person_id, []).append(item)

    waiting: List[WaitingPersonOut] = []
    for person_id, group in by_person.items():
        person = db.get_person(person_id) if person_id else None
        waiting.append(
            WaitingPersonOut(
                person_id=person_id,
                person=person.display_name if person else group[0].person,
                tie_strength=relationships.strength(person_id) if person_id else 0.0,
                tie_label=relationships.describe(person_id) if person_id else "unknown sender",
                waited_since=min(i.timestamp for i in group),
                open_count=len(group),
                top_item=ItemOut.of(group[0]),
            )
        )
    waiting.sort(key=lambda w: (-w.tie_strength, w.waited_since))

    # "One thing now" = the most pressing decision: time-sensitive first, then
    # what's due soon, then importance — so a thing due today beats a high-scoring
    # thing due in a month.
    order = sorted(
        actionable,
        key=lambda i: (
            InterruptionLevel.ORDER.get(i.interruption_level, 1),
            0 if _due_days(i) <= 2 else 1,
            -i.score,
        ),
    )
    one_now = order[0] if order else (items[0] if items else None)

    return BriefingOut(
        generated_at=reference.isoformat(timespec="seconds"),
        mode=mode,
        caught_up=not items,
        one_now=ItemOut.of(one_now) if one_now else None,
        waiting=waiting[:8],
    )


@app.post("/calendar/events")
def calendar_sync(body: CalendarSyncIn) -> Dict[str, int]:
    """Device calendar (EventKit) → context. The phone pushes its events here so
    the completion engine can match items against them and the model of you knows
    what's on your schedule. Runs a completion scan so anything now on your
    calendar can auto-close right away."""
    events = [
        CalendarEvent(
            id=e.id, calendar_id=e.calendar_id, summary=e.summary,
            description=e.description, location=e.location,
            start_at=e.start_at, end_at=e.end_at, self_response=e.self_response,
        )
        for e in body.events
    ]
    stored = db.upsert_calendar_events(events)
    engine.scan()
    return {"stored": stored}


def _receipts_from_calls(calls: List[Dict[str, Any]]) -> List[ReceiptOut]:
    """The sources an answer rests on, pulled from what the loop actually
    read (§v2.9). A message opened in full is the strongest receipt; search
    hits it saw come after. Capped, newest-mention first, deduped."""
    import json as _j

    seen: Dict[str, ReceiptOut] = {}
    order: List[str] = []
    for call in calls:
        try:
            result = _j.loads(call.get("result") or "null")
        except ValueError:
            continue
        hits = []
        if isinstance(result, dict) and result.get("message_id"):
            hits = [result]
        elif isinstance(result, list):
            hits = [h for h in result if isinstance(h, dict) and h.get("message_id")][:3]
        for hit in hits:
            mid = hit["message_id"]
            label = (hit.get("subject") or (hit.get("text") or "")[:48]).strip() or "message"
            when = (hit.get("timestamp") or "")[:10]
            receipt = ReceiptOut(
                kind="message", ref_id=mid, source=hit.get("source") or "",
                label=f"{label} · {when}" if when else label,
                detail=(hit.get("via_attachment") or ""),
            )
            if mid in seen:
                order.remove(mid)
            seen[mid] = receipt
            order.append(mid)
    return [seen[mid] for mid in reversed(order)][:6]


@app.post("/ask", response_model=AskOut)
def ask(body: AskIn) -> AskOut:
    """Ask the assistant about your world (§v2.9: the answer is a card).

    The agentic loop decides which read tools to call and investigates until
    it can answer; the response carries the receipts it rests on, the facts
    the world model contributed (correctable by id), and the trace. Falls
    back to the RAG-lite path when no tool-capable provider is available."""
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="empty question")

    # §v2.8 phase 4 — resolve names before any search runs. "Nora" becomes a
    # person whose facts supply the vocabulary the search needs.
    from .. import world
    prompt = question
    grounding = world.grounding_data(question)
    known_block = world.grounding(question)
    if known_block:
        prompt = f"{known_block}\n\n{question}"
    knew = [
        KnownFactOut(fact_id=f.id, entity_id=entry["entity_id"], entity=entry["name"],
                     predicate=f.predicate, value=f.value)
        for entry in grounding for f in entry["facts"]
    ][:8]

    run = assistant_loop.run_loop(
        prompt, trigger="ask", system=assistant_tools.ASSISTANT_LOOP_SYSTEM,
        max_iterations=8,
    )
    if run and run.conclusion:
        out = AskOut(
            id=new_id(),
            question=question,
            answer=run.conclusion,
            sources=sorted({c["name"] for c in run.tool_calls}),
            receipts=_receipts_from_calls(run.tool_calls),
            knew=knew,
            trace=_trace(run.tool_calls),
            created_at=now_iso(),
        )
        db.save_ask({
            "id": out.id, "question": question, "answer": out.answer,
            "receipts": [r.model_dump() for r in out.receipts],
            "knew": [k.model_dump() for k in out.knew],
            "trace": [t.model_dump() for t in out.trace],
            "created_at": out.created_at,
        })
        return out

    # RAG-lite fallback: fixed tool sweep + one completion.
    context = assistant_tools.gather_context(question)
    answer: Optional[str] = None
    text = providers.run(
        lambda p: p.complete_json(
            assistant_tools.build_prompt(question, context),
            system=assistant_tools.ASSISTANT_SYSTEM,
            max_tokens=400,
        ),
        "assistant answer",
    )
    if text:
        try:
            answer = _parse_json(text).get("answer")
        except Exception:
            answer = None
    if not answer:
        answer = assistant_tools.fallback_answer(question, context)
    return AskOut(answer=answer, sources=context.sources())


@app.get("/messages/{message_id}", response_model=Dict[str, Any])
def message_in_full(message_id: str) -> Dict[str, Any]:
    """One message, whole — what a receipt chip opens (§v2.9)."""
    message = db.get_message(message_id)
    if message is None:
        raise HTTPException(404, f"no message {message_id}")
    person = db.get_person(message.person_id) if message.person_id else None
    return {
        "message_id": message.id,
        "source": message.source,
        "sender": "You" if message.is_from_user else (person.display_name if person else "someone"),
        "timestamp": message.timestamp,
        "subject": message.metadata.get("subject") or "",
        "text": message.text,
        "attachments": [
            {"filename": a.filename, "has_text": bool(a.text)}
            for a in db.attachments_for_message(message.id)
        ],
    }


@app.get("/asks", response_model=List[AskOut])
def ask_history(limit: int = 20) -> List[AskOut]:
    """Past answer cards, newest first — the Ask surface is a reference."""
    return [AskOut(**row, sources=[]) for row in db.list_asks(limit=limit)]


@app.post("/world/facts/{fact_id}", response_model=Dict[str, str])
def correct_world_fact(fact_id: str, body: Dict[str, str]) -> Dict[str, str]:
    """§v2.9 — the "wrong?" door. The user's word beats the model's:
    action=forget retires the fact; action=correct with a value retires it
    and records the user's version at full confidence. Superseded, never
    deleted."""
    from .. import world

    action = (body.get("action") or "").strip()
    if action not in ("forget", "correct"):
        raise HTTPException(400, "action must be 'forget' or 'correct'")
    result = world.correct_fact(fact_id, action, value=body.get("value"))
    if result is None:
        raise HTTPException(404, f"no fact {fact_id}")
    return {"status": "superseded" if action == "forget" else "corrected"}


@app.post("/tell", response_model=TellOut)
def tell(body: TellIn) -> TellOut:
    """Tell the assistant something about your life (§v1.4 pillar B). The loop
    extracts durable facts into the model of you and echoes back what it
    captured. Without a tool-capable provider the statement is saved whole, so
    nothing the user tells us is ever dropped."""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")

    recorded: List[Fact] = []
    tools = [t for t in assistant_registry.READ_TOOLS if t.name in ("find_person",)]
    tools += assistant_registry.fact_tools(recorded)
    run = assistant_loop.run_loop(
        f"The user says: {text}", trigger="tell", tools=tools, system=assistant_tools.TELL_SYSTEM
    )

    if not recorded and (run is None or not run.conclusion):
        # Offline / failed: keep the user's words verbatim as one self-fact.
        recorded.append(
            db.upsert_fact(Fact(subject_type="self", statement=text, provenance="tell"))
        )
    reply = (run.conclusion if run and run.conclusion else None) or "Noted."
    return TellOut(reply=reply, facts=[_fact_out(f) for f in recorded])


def _fact_out(f: Fact) -> FactOut:
    return FactOut(
        id=f.id,
        subject_type=f.subject_type,
        subject_id=f.subject_id,
        statement=f.statement,
        predicate=f.predicate,
        value=f.value,
        source=f.source,
        confidence=f.confidence,
        status=f.status,
        updated_at=f.updated_at,
    )


@app.post("/converse", response_model=ConverseOut)
def converse(body: ConverseIn) -> ConverseOut:
    """§v1.5 — the one conversational door. The loop decides whether you told
    it something (facts recorded) or asked it something (investigated with the
    read tools), and the trace shows every tool it fired — receipts, not a
    spinner. Degrades to /tell-verbatim or the RAG-lite answer offline."""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")

    # Continue the caller's conversation, or open a new one. History is what
    # lets "her" and a bare "Katie" resolve — without it every turn is turn zero.
    session_id = body.session_id or new_id()
    history = [
        {"role": t["role"], "text": t["text"]}
        for t in db.conversation_turns(session_id, limit=HISTORY_TURNS)
    ]
    db.add_turn(session_id, "user", text)

    recorded: List[Fact] = []
    drafted: List[dict] = []
    tools = (
        list(assistant_registry.READ_TOOLS)
        + assistant_registry.fact_tools(recorded)
        + assistant_registry.draft_tools(drafted)
    )
    run = assistant_loop.run_loop(
        text, trigger="converse", tools=tools, system=assistant_tools.CONVERSE_SYSTEM,
        max_iterations=8,   # thorough beats fast on the loop model (it's cheap now)
        history=history, session_id=session_id,
    )
    if run and run.conclusion:
        facts_out = [_fact_out(f) for f in recorded]
        trace_out = _trace(run.tool_calls)
        draft_out = DraftOut(**drafted[-1]) if drafted else None
        db.add_turn(
            session_id, "assistant", run.conclusion,
            facts=[f.model_dump() for f in facts_out],
            trace=[t.model_dump() for t in trace_out],
        )
        return ConverseOut(
            reply=run.conclusion, session_id=session_id, facts=facts_out,
            draft=draft_out, trace=trace_out,
        )

    # Offline / failed. Nothing the user says is ever dropped: questions get
    # the plain tool report, and any non-question sentences are kept verbatim —
    # including in mixed input ("I moved my gym to mornings. What do I owe Tess?").
    sentences = re.split(r"(?<=[.!?])\s+", text)
    statements = " ".join(s for s in sentences if s and not s.rstrip().endswith("?")).strip()
    questions = any(s.rstrip().endswith("?") for s in sentences)

    facts: List[Fact] = []
    if statements:
        facts.append(db.upsert_fact(Fact(subject_type="self", statement=statements, provenance="tell")))
    reply = "Noted."
    if questions:
        context = assistant_tools.gather_context(text)
        answer = assistant_tools.fallback_answer(text, context)
        reply = f"Noted. {answer}" if facts else answer
    facts_out = [_fact_out(f) for f in facts]
    db.add_turn(session_id, "assistant", reply, facts=[f.model_dump() for f in facts_out])
    return ConverseOut(reply=reply, session_id=session_id, facts=facts_out)


HISTORY_TURNS = 20     # ~10 exchanges of memory; older turns fall off the window


@app.get("/converse/{session_id}", response_model=ConversationOut)
def conversation(session_id: str) -> ConversationOut:
    """A conversation's transcript — so the app reopens where you left off."""
    return ConversationOut(
        session_id=session_id,
        turns=[TurnOut(**t) for t in db.conversation_turns(session_id, limit=HISTORY_TURNS)],
    )


def _trace(calls: List[Dict[str, Any]]) -> List[TraceStepOut]:
    """Fold the loop's call log into user-facing receipts."""
    import json as _j

    steps: List[TraceStepOut] = []
    for call in calls:
        hint = next(
            (str(v) for k, v in (call.get("input") or {}).items()
             if k in ("query", "name", "statement") and v),
            "",
        )
        ok, found = True, ""
        try:
            result = _j.loads(call.get("result") or "null")
        except ValueError:
            result = None
        if isinstance(result, dict):
            if "error" in result:
                ok, found = False, "nothing found"
            elif "recorded" in result:
                found = f"saved: {result.get('statement', '')[:60]}"
            elif "created" in result:
                found = f"surfaced: {result.get('headline', '')[:60]}"
            else:
                found = ", ".join(list(result)[:3])
        elif isinstance(result, list):
            ok = bool(result)
            found = f"{len(result)} result{'s' if len(result) != 1 else ''}" if result else "nothing found"
        elif result is None:
            ok, found = False, "nothing found"
        summary = f"{hint} · {found}" if hint and found else (found or hint or "checked")
        steps.append(TraceStepOut(tool=call["name"], summary=summary, ok=ok))
    return steps


@app.get("/model", response_model=ModelOut)
def model_of_you() -> ModelOut:
    """§v1.4 — what the system believes about you, grouped and editable."""
    facts = db.list_facts()
    you = [_fact_out(f) for f in facts if f.subject_type == "self"]

    people: Dict[str, List[Fact]] = {}
    topics: Dict[str, List[Fact]] = {}
    for f in facts:
        if f.subject_type == "person":
            people.setdefault(f.subject_id or "?", []).append(f)
        elif f.subject_type == "topic":
            topics.setdefault(f.subject_id or f.statement, []).append(f)

    people_out = []
    for pid, fs in people.items():
        person = db.get_person(pid)
        people_out.append(
            PersonFactsOut(
                person_id=pid,
                name=person.display_name if person else pid,
                tie_label=relationships.describe(pid),
                facts=[_fact_out(f) for f in fs],
            )
        )
    topics_out = [
        PersonFactsOut(person_id=None, name=slug, facts=[_fact_out(f) for f in fs])
        for slug, fs in topics.items()
    ]
    return ModelOut(you=you, people=people_out, topics=topics_out)


@app.patch("/facts/{fact_id}", response_model=FactOut)
def edit_fact(fact_id: str, body: FactEditIn) -> FactOut:
    """A user correction — becomes the authoritative version of the fact."""
    fact = db.get_fact(fact_id)
    if not fact:
        raise HTTPException(404, f"no fact {fact_id}")
    if body.statement is not None:
        fact.statement = body.statement
    if body.predicate is not None:
        fact.predicate = body.predicate
    if body.value is not None:
        fact.value = body.value
    if body.status is not None:
        if body.status not in ("active", "dismissed"):
            raise HTTPException(400, "status must be active|dismissed")
        fact.status = body.status
    fact.source = "user"
    fact.confidence = 1.0
    fact.provenance = "user-edit"
    return _fact_out(db.upsert_fact(fact))


@app.delete("/facts/{fact_id}", response_model=FactOut)
def dismiss_fact(fact_id: str) -> FactOut:
    """Soft delete — the dismissal itself is signal, so the row stays."""
    fact = db.get_fact(fact_id)
    if not fact:
        raise HTTPException(404, f"no fact {fact_id}")
    fact.status = "dismissed"
    fact.provenance = "user-edit"
    return _fact_out(db.upsert_fact(fact))


@app.get("/conversations", response_model=List[ConversationSummaryOut])
def conversations() -> List[ConversationSummaryOut]:
    """§8.2 Threads — per-person, all pending items from that conversation."""
    open_items = scorer.ranked(include_snoozed=True)
    by_person: Dict[Optional[str], List] = {}
    for item in open_items:
        by_person.setdefault(item.person_id, []).append(item)

    summaries = []
    for person_id, items in by_person.items():
        person = db.get_person(person_id) if person_id else None
        top = min(items, key=lambda i: InterruptionLevel.ORDER.get(i.interruption_level, 1))
        summaries.append(
            ConversationSummaryOut(
                person_id=person_id,
                person=person.display_name if person else items[0].person,
                relationship=person.relationship if person else None,
                sources=sorted({i.source for i in items}),
                open_count=len(items),
                top_level=top.interruption_level,
                last_activity=max(i.timestamp for i in items),
                topic=_thread_topic(person_id, items),
                tie_strength=relationships.strength(person_id) if person_id else 0.0,
            )
        )
    summaries.sort(key=lambda s: (InterruptionLevel.ORDER.get(s.top_level, 1), -s.open_count))
    return summaries


def _thread_topic(person_id: Optional[str], items: List) -> Optional[str]:
    """A short "what this is about" label, cached per thread and only
    recomputed when the thread's open items change."""
    key = person_id or (items[0].person if items else "unknown")
    signature = str(sorted(i.id for i in items))
    cached = db.get_sync_state(f"topic:v2:{key}")
    if cached:
        sig, _, title = cached.partition("\n")
        if sig == signature and title:
            return title

    # Cache miss: return the instant heuristic but DON'T write it — the
    # poll-time LLM digest (topics.refresh_thread_topics) owns the cache, so a
    # request-path heuristic never blocks a real title from landing later.
    snippets = [i.suggested_action or i.raw_text for i in items]
    person = items[0].person if items else "someone"
    return topics.heuristic_title(person, snippets)


@app.get("/conversations/{person_id}", response_model=List[ItemOut])
def thread_items(person_id: str) -> List[ItemOut]:
    items = [i for i in db.list_items(person_id=person_id) if i.status in ("pending", "snoozed")]
    items.sort(key=lambda i: (InterruptionLevel.ORDER.get(i.interruption_level, 1), -i.score))
    return [ItemOut.of(i) for i in items]


def _staged_items(person_id: str, item_ids: List[str]) -> List:
    """The requested items that are genuinely this person's and still open —
    the server decides what's foldable, the client only proposes."""
    wanted = set(item_ids)
    items = [
        i for i in db.list_items(person_id=person_id)
        if i.id in wanted and i.status in ("pending", "snoozed")
    ]
    # Preserve the caller's ordering (that's the order they read them in).
    order = {iid: n for n, iid in enumerate(item_ids)}
    items.sort(key=lambda i: order.get(i.id, 1_000))
    return items


@app.post("/conversations/{person_id}/draft", response_model=DraftBatchOut)
def draft_batch_reply(person_id: str, body: BatchIn) -> DraftBatchOut:
    """Phase D — fold the staged items owed to this person into one reply."""
    items = _staged_items(person_id, body.item_ids)
    if not items:
        raise HTTPException(status_code=404, detail="no open items for that person")
    person = db.get_person(person_id)
    name = person.display_name if person else items[0].person
    reply = batching.compose_reply(name, items)
    handle = next((h for i in items if (h := _counterpart_handle(i))), None)
    return DraftBatchOut(reply=reply, handle=handle, item_ids=[i.id for i in items])


@app.post("/items/batch/done", response_model=BatchDoneOut)
def batch_done(body: BatchIn) -> BatchDoneOut:
    """Sending one reply clears every item it covered."""
    closed: List[str] = []
    for item_id in body.item_ids:
        item = engine.manual_close(item_id)
        if item:
            closed.append(item.id)
    return BatchDoneOut(completed=closed)


@app.get("/history", response_model=HistoryOut)
def history(limit: int = Query(100, le=500)) -> HistoryOut:
    """§8.2 History — what closed, and how it closed."""
    completed = db.list_items(status="completed")
    completed.sort(key=lambda i: i.completed_at or i.updated_at, reverse=True)
    completed = completed[:limit]

    entries = []
    for item in completed:
        signals = [s for s in db.signals_for_item(item.id) if s.resolution in ("auto_closed", "confirmed")]
        evidence = signals[0] if signals else None
        entries.append(
            HistoryEntryOut(
                item=ItemOut.of(item),
                closed_by=item.completed_by or "manual",
                closed_at=item.completed_at,
                evidence="; ".join(evidence.reasons) if evidence else None,
                evidence_source=evidence.source if evidence else None,
            )
        )

    days = {(i.completed_at or "")[:10] for i in completed if i.completed_at}
    streak, cursor = 0, datetime.now(timezone.utc)
    while cursor.strftime("%Y-%m-%d") in days:
        streak += 1
        cursor -= timedelta(days=1)

    return HistoryOut(
        entries=entries,
        auto_closed=sum(1 for i in completed if i.completed_by == "auto"),
        manual_closed=sum(1 for i in completed if i.completed_by != "auto"),
        streak_days=streak,
    )


@app.get("/items/{item_id}", response_model=ItemOut)
def item_detail(item_id: str) -> ItemOut:
    return ItemOut.of(_item_or_404(item_id))


@app.get("/items/{item_id}/enriched")
def item_enriched(item_id: str) -> Dict[str, object]:
    """The context engine's read on an item: a grounded headline + briefing built
    from what it found (frequency, relationship, conversation), not a template."""
    return assistant_enrich.enrich_item(_item_or_404(item_id))


@app.get("/items/{item_id}/dossier", response_model=DossierOut)
def item_dossier(item_id: str) -> DossierOut:
    """The receipts: why this surfaced, the surrounding conversation, and — the
    memory-jog — your own last words in it (plus whether you're the one who spoke
    last and never heard back)."""
    item = _item_or_404(item_id)

    reasons = sorted(item.score_explanation, key=lambda e: -abs(e.get("contribution", 0.0)))
    why = [
        e["detail"] for e in reasons
        if e.get("detail") and e.get("contribution", 0) > 0
        and "learned weight" not in e["detail"]      # skip the model-internals jargon
    ][:4]

    def _who(m) -> str:
        return "You" if m.is_from_user else item.person

    def _out(m, pivot_id: Optional[str] = None) -> MessageOut:
        return MessageOut(id=m.id, sender=_who(m), is_from_user=m.is_from_user,
                          timestamp=m.timestamp, text=m.text, is_pivot=(m.id == pivot_id))

    last_word = db.last_user_message(item.conversation_id) if item.conversation_id else None
    tail = db.last_message(item.conversation_id) if item.conversation_id else None
    awaiting = bool(tail and tail.is_from_user)

    pivot = db.get_message(item.message_id) if item.message_id else None
    pivot_ts = pivot.timestamp if pivot else item.timestamp
    thread = db.message_context(item.conversation_id, pivot_ts, before=4, after=2) if item.conversation_id else []

    return DossierOut(
        why=why,
        your_last_word=_out(last_word) if last_word else None,
        awaiting_reply=awaiting,
        messages=[_out(m, pivot.id if pivot else None) for m in thread],
    )


@app.get("/items/{item_id}/context", response_model=ConversationContextOut)
def item_context(item_id: str, before: int = 4, after: int = 2) -> ConversationContextOut:
    """The surrounding conversation for a surfaced item — a few messages of
    back-and-forth so the item isn't an orphaned line. iMessage can't deep-link
    to a specific message, but we own the thread, so we bring it to the card."""
    item = _item_or_404(item_id)
    pivot = db.get_message(item.message_id) if item.message_id else None
    pivot_ts = pivot.timestamp if pivot else item.timestamp
    msgs = db.message_context(item.conversation_id, pivot_ts, before=before, after=after)

    def sender_name(m) -> str:
        if m.is_from_user:
            return "You"
        if m.person_id:
            p = db.get_person(m.person_id)
            if p and p.display_name:
                return p.display_name
        return item.person

    out = [
        MessageOut(
            id=m.id,
            sender=sender_name(m),
            is_from_user=m.is_from_user,
            timestamp=m.timestamp,
            text=m.text,
            is_pivot=(pivot is not None and m.id == pivot.id),
        )
        for m in msgs
    ]
    return ConversationContextOut(
        item_id=item.id, conversation_id=item.conversation_id, person=item.person, messages=out
    )


# ---------------------------------------------------------------- actions
@app.post("/items/{item_id}/view", response_model=ActionOut)
def item_viewed(item_id: str, expanded: bool = False) -> ActionOut:
    """§6.2 revisit tracking — the input to the avoidance/deprioritization read."""
    item = _item_or_404(item_id)
    db.log_behavior("expanded" if expanded else "viewed", item_id=item.id)
    return ActionOut(item=ItemOut.of(item))


@app.post("/items/{item_id}/act", response_model=ActionOut)
def item_acted(item_id: str) -> ActionOut:
    """The user did something with it without closing it (opened a link, replied)."""
    item = _item_or_404(item_id)
    learning.record("acted", item)
    scorer.score_item(item)
    db.save_item(item)
    return ActionOut(item=ItemOut.of(item))


@app.post("/items/{item_id}/done", response_model=ActionOut)
def item_done(item_id: str) -> ActionOut:
    item = engine.manual_close(item_id)
    if not item:
        raise HTTPException(404, f"no item {item_id}")
    return ActionOut(item=ItemOut.of(item))


@app.post("/items/{item_id}/snooze", response_model=ActionOut)
def item_snooze(item_id: str, body: SnoozeIn) -> ActionOut:
    item = _item_or_404(item_id)
    if body.until:
        wake = body.until
    else:
        hours = body.hours if body.hours is not None else 24.0
        wake = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
    item.status = "snoozed"
    item.snoozed_until = wake
    db.save_item(item)
    learning.record("snoozed", item, payload={"until": wake})
    return ActionOut(item=ItemOut.of(item), detail=f"snoozed until {wake}")


@app.post("/items/{item_id}/dismiss", response_model=ActionOut)
def item_dismiss(item_id: str) -> ActionOut:
    """Explicit "stop showing me this" — the strongest deprioritization signal."""
    item = _item_or_404(item_id)
    item.status = "dismissed"
    db.save_item(item)
    learning.record("dismissed", item)
    return ActionOut(item=ItemOut.of(item))


# ---------------------------------------------------------- confirmations
@app.get("/confirmations", response_model=List[ConfirmationOut])
def confirmations() -> List[ConfirmationOut]:
    return _confirmations()


@app.post("/confirmations/{signal_id}/confirm", response_model=ActionOut)
def confirm(signal_id: str) -> ActionOut:
    item = engine.confirm(signal_id)
    if not item:
        raise HTTPException(404, "no open confirmation with that id")
    return ActionOut(item=ItemOut.of(item), detail="closed")


@app.post("/confirmations/{signal_id}/reject", response_model=ActionOut)
def reject(signal_id: str) -> ActionOut:
    item = engine.reject(signal_id)
    if not item:
        raise HTTPException(404, "no confirmation with that id")
    return ActionOut(item=ItemOut.of(item), detail="kept open")


# --------------------------------------------------------------- threads
# §v2 step 1. `/threads` is the main stack — live + quiet, nothing else. What
# the system merely *proposed* lives at `/proposals` and reaches the stack only
# when the user accepts it. That separation is what makes the thread count mean
# something, so it is enforced here rather than left to the client.
def _thread_out(thread, reference: Optional[datetime] = None) -> ThreadOut:
    """One lane, fully rendered. The stripe, the badge, and the track are all
    decided here rather than on the client — same rule the interruption levels
    already follow."""
    out = ThreadOut.of(thread, evidence_count=len(db.thread_evidence(thread.id)))
    out.lane = threads_mod.lane_state(thread, reference)
    out.subtitle = threads_mod.subtitle(thread)
    out.unseen = threads_mod.unseen_count(thread)
    out.activity = [ActivityMarkOut(**m) for m in threads_mod.activity(thread.id)]
    out.status = threads_mod.status(thread, reference)
    out.status_label = threads_mod.Status.LABEL.get(out.status)
    reason = threads_mod.why(thread, reference)
    if reason:
        out.why_kind, out.why_text = reason["kind"], reason["text"]
    return out


def _evidence_out(rows: List[Dict[str, Any]]) -> List[EvidenceOut]:
    return [EvidenceOut(**r) for r in rows]


def _thread_error(exc: threads_mod.ThreadError) -> HTTPException:
    """A missing thread is a 404; anything else the model forbids is a 400."""
    text = str(exc)
    return HTTPException(404 if text.startswith("no thread") or text.startswith("no item") else 400, text)


@app.get("/threads", response_model=ThreadStackOut)
def thread_stack(state: Optional[str] = None, at: Optional[str] = None) -> ThreadStackOut:
    """**The main view.** What you're carrying, plus what you just put down.

    Resolved threads ride along for 24 hours, struck through, then the poller
    archives them. That is deliberate: a stack that only ever grows is the
    failure mode this product exists to avoid, so the moment it shrinks has to
    be something you can see.

    `state=all` includes archived; proposals never appear here even when asked
    for by name — that's what `/proposals` is.
    """
    reference = parse_iso(at) or datetime.now(timezone.utc)
    if state == "all":
        states = [s for s in ThreadState.ALL if s != ThreadState.PROPOSED]
        rows = db.list_threads(states=states, reference=reference)
    elif state:
        if state == ThreadState.PROPOSED:
            raise HTTPException(400, "proposals live at /proposals, not in the stack")
        if state not in ThreadState.ALL:
            raise HTTPException(400, f"unknown state {state}")
        rows = db.list_threads(states=[state], reference=reference)
    else:
        rows = threads_mod.stack(reference)

    tally = threads_mod.counts(reference)
    # Tiers are assigned across the whole set, not per thread — "the lead"
    # only means something relative to everything else on the page.
    tiers = threads_mod.tiers(rows, reference)
    out = []
    for t in rows:
        item = _thread_out(t, reference)
        item.tier = tiers.get(t.id, threads_mod.Tier.INDEX)
        item.pressure = threads_mod.pressure(t, reference)
        out.append(item)

    return ThreadStackOut(
        generated_at=reference.isoformat(timespec="seconds"),
        running=tally["running"],
        needs_you=tally["needs_you"],
        threads=out,
    )


@app.post("/threads", response_model=ThreadOut)
def create_thread(body: ThreadIn, background: BackgroundTasks) -> ThreadOut:
    """Declare a thread. This is the primary capture verb of v2 — the user
    saying "this is on my mind" without waiting for a message to imply it."""
    try:
        thread = threads_mod.create(
            title=body.title,
            summary=body.summary,
            state=body.state,
            importance=body.importance,
            contact_person_id=body.contact_person_id,
        )
        if body.deadline:
            # Typed by the user, so it needs no evidence and outranks anything
            # the system later infers.
            thread = threads_mod.set_deadline(thread.id, body.deadline, source="user")
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)

    # The raw sentence is the declaration, not the headline — the feed title
    # is generated from context, after this response, so declare stays
    # instant and the arrival animation still flies the typed words. The
    # headline lands on the next stack refresh.
    from ..threads import titles as thread_titles
    background.add_task(thread_titles.retitle, thread.id)

    # The client animates the new thread into the place it is going to occupy,
    # so this response has to know that place. Without it the row arrives as a
    # default `index` and then jumps a tier the moment the stack reloads —
    # which is the lie the arrival animation exists to stop telling.
    reference = datetime.now(timezone.utc)
    out = _thread_out(thread, reference)
    out.tier = threads_mod.tiers(threads_mod.stack(reference), reference).get(
        thread.id, threads_mod.Tier.INDEX
    )
    out.pressure = threads_mod.pressure(thread, reference)
    return out


# ------------------------------------------------------- closing the loop
@app.get("/threads/closures", response_model=List[ThreadClosureOut])
def thread_closures() -> List[ThreadClosureOut]:
    """Threads the evidence *suggests* are finished, but not definitely enough
    to close on the user's behalf. The stricter sibling of `/confirmations`:
    v1.5's item-level ask band was answered 39 times and was wrong 37 of them,
    so a thread is only ever asked about on structural evidence."""
    out = []
    for record in db.pending_thread_closures():
        thread = db.get_thread(record["thread_id"])
        if not thread or thread.state in ThreadState.CLOSED:
            continue
        out.append(ThreadClosureOut(
            id=record["id"],
            thread=_thread_out(thread),
            confidence=record["confidence"],
            reasons=record["reasons"],
            evidence=_evidence_out([
                threads_mod._resolve_ref(e.get("kind", "item"), e.get("ref_id", ""))
                or threads_mod._tombstone(e.get("kind", "item"), e.get("ref_id", ""))
                for e in record["evidence"]
            ]),
            detected_at=record["detected_at"],
        ))
    return out


@app.post("/threads/closures/{closure_id}/confirm", response_model=ThreadOut)
def confirm_thread_closure(closure_id: str) -> ThreadOut:
    thread = thread_closure_engine.confirm(closure_id)
    if not thread:
        raise HTTPException(404, "no open closure with that id")
    return _thread_out(thread)


@app.post("/threads/closures/{closure_id}/reject", response_model=ThreadOut)
def reject_thread_closure(closure_id: str) -> ThreadOut:
    """The user says no. The thread stays open, and the rejection is kept —
    a wrong guess the user corrected is the most informative row there is."""
    thread = thread_closure_engine.reject(closure_id)
    if not thread:
        raise HTTPException(404, "no closure with that id")
    return _thread_out(thread)


# NOTE: these are declared before `/threads/{thread_id}` on purpose.
# FastAPI matches in declaration order, so a static segment placed after
# the parameterised one is swallowed by it — `/threads/closures` returned
# "no thread closures" for a thread whose id was literally "closures".
@app.get("/threads/{thread_id}", response_model=ThreadDetailOut)
def thread_detail(thread_id: str) -> ThreadDetailOut:
    """The thread and every piece of evidence it has claimed, including the
    receipts behind an inferred deadline."""
    thread = db.get_thread(thread_id)
    if not thread:
        raise HTTPException(404, f"no thread {thread_id}")
    evidence = threads_mod.evidence_for(thread.id)
    out = ThreadOut.of(thread, evidence_count=len(evidence))
    if out.deadline:
        receipt = threads_mod.deadline_receipt(thread.id)
        out.deadline = DeadlineOut(
            date=receipt["deadline"],
            source=receipt["source"],
            reason=receipt["reason"],
            evidence=_evidence_out(receipt["evidence"]),
        )
    findings = [
        FindingOut(**{**f, "evidence": _evidence_out(f["evidence"])})
        for f in threads_mod.findings_for(thread.id)
    ]
    watching = [
        WatcherOut(
            id=w.id, kind=w.kind, what=w.what, every_minutes=w.cadence_minutes,
            until=w.until, times_fired=w.fire_count, last_fired_at=w.last_fired_at,
        )
        for w in thread_watchers_mod.for_thread(thread.id)
    ]
    return ThreadDetailOut(
        thread=out, evidence=_evidence_out(evidence),
        findings=findings, watchers=watching,
        corrections=[
            CorrectionOut(id=f.id, statement=f.statement, said_at=f.updated_at)
            for f in threads_mod.corrections(thread.id)
        ],
    )


@app.get("/interruption")
def interruption_state() -> Dict[str, Any]:
    """The bet the system is placing on the user's attention: the current bar,
    what it started at, and how much of today's interruption budget is spent."""
    from ..notifications import interruption

    return interruption.state()


@app.post("/threads/{thread_id}/seen", response_model=ThreadOut)
def mark_thread_seen(thread_id: str) -> ThreadOut:
    """The user opened it, so nothing in it is new any more. Explicit rather
    than a side effect of GET — a read shouldn't quietly clear a badge the
    user may not have looked at."""
    try:
        return _thread_out(threads_mod.mark_seen(thread_id))
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)


@app.patch("/threads/{thread_id}", response_model=ThreadOut)
def edit_thread(thread_id: str, body: ThreadEditIn) -> ThreadOut:
    """Title, summary, importance, state. The swipe verbs land here: quiet is
    a state, dig-in is importance, resolve has its own route because who closed
    a loop is part of the record."""
    if body.state == ThreadState.PROPOSED:
        raise HTTPException(400, "a thread cannot be demoted back to a proposal")
    try:
        thread = threads_mod.update(
            thread_id,
            title=body.title,
            summary=body.summary,
            state=body.state,
            importance=body.importance,
        )
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)
    return _thread_out(thread)


@app.post("/threads/{thread_id}/quiet", response_model=ThreadOut)
def quiet_thread(thread_id: str) -> ThreadOut:
    """← Quiet. Keeps working, stops surfacing — and raises the interruption
    bar, because what was wrong was the timing, not the thread.

    Its own route rather than a PATCH: the swipe verbs are the training signal
    (§v2 7b/7c), and inferring which verb was meant from the shape of a PATCH
    body is a guess. `{"importance": 0.9, "state": "quiet"}` is genuinely
    ambiguous, and a signal read from an ambiguous request teaches noise.
    """
    try:
        return _thread_out(threads_mod.quiet(thread_id))
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)


@app.post("/threads/{thread_id}/dig-in", response_model=ThreadOut)
def dig_in_thread(thread_id: str) -> ThreadOut:
    """↑ Dig in. Raises this thread's importance and lowers the bar globally —
    the user is asking to hear sooner, not only about this one."""
    try:
        return _thread_out(threads_mod.dig_in(thread_id))
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)


@app.post("/threads/{thread_id}/resolve", response_model=ThreadOut)
def resolve_thread(thread_id: str) -> ThreadOut:
    """Close a loop by hand. The measure of this product is that this number
    goes up. (Evidence-based closure is step 5 and writes resolved_by=evidence
    through the same door.)"""
    try:
        thread = threads_mod.resolve(thread_id, by="user")
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)
    return _thread_out(thread)


@app.post("/threads/{thread_id}/autonomy", response_model=ThreadOut)
def set_thread_autonomy(thread_id: str, body: AutonomyIn) -> ThreadOut:
    """Set how far this thread may go on its own.

    **User-set, and never learned.** `prepared` already needs no permission, so
    the only rung learning could promote *into* is `ask` — spending money,
    things that are hard to undo — which would turn unrelated draft approvals
    into consent for irreversible acts. Learning may lower a thread; only the
    user raises one. That is why this is an endpoint and not a signal.
    """
    try:
        return _thread_out(threads_mod.set_autonomy(thread_id, body.ceiling))
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)


@app.post("/threads/{thread_id}/findings/{finding_id}/reject")
def reject_move(thread_id: str, finding_id: str, body: Optional[RejectIn] = None) -> Dict[str, object]:
    """"Not this" — but which kind of no?

    A rejected move costs the user more than an ignored finding: it asked them
    to overrule the system rather than merely skim it. Until v2.4 that was the
    *only* thing a move could hear, and it always meant the same thing — fewer
    moves of this shape on threads like this one. Three unrelated intents
    collapsed into it, and since `may_propose` only ever narrows, each one
    could permanently close a door:

      handled   the user did it first. The move was right and the timing was
                not, so it must teach nothing at all — this is the case that
                pushed `move:gather` on the hoop thread to 0.45 against a 0.40
                floor for a reason that was merely stale.
      wrong     the system misread the job. The user is giving a far better
                signal than a thumbs-down, so it is recorded as a correction on
                the thread and the appetite is left alone. Punishing the shape
                here would teach the opposite of what they meant: on the
                pajamas thread `decide` was correct and under-decisive.
      unwanted  the original behaviour, kept and now chosen deliberately.
    """
    thread = db.get_thread(thread_id)
    finding = db.get_finding(finding_id)
    if thread is None or finding is None:
        raise HTTPException(404, "no such thread or finding")

    body = body or RejectIn()
    if body.reason not in ("handled", "wrong", "unwanted"):
        raise HTTPException(400, "reason must be handled, wrong or unwanted")
    if body.reason == "wrong" and not (body.text or "").strip():
        raise HTTPException(400, "reason 'wrong' needs the text of what was wrong")

    db.dismiss_finding(finding_id)
    recorded = None

    if body.reason == "unwanted":
        learning.record_move("rejected", thread, finding.move_kind)
    elif body.reason == "wrong":
        recorded = threads_mod.correct(thread_id, body.text or "")
    elif body.reason == "handled":
        # They got there first, so the thread is done — and nothing about the
        # system's judgement was wrong, so nothing is learned from it.
        threads_mod.resolve(thread_id, by="user")

    return {
        "rejected": finding_id,
        "reason": body.reason,
        "correction_id": recorded.id if recorded else None,
        "appetite": learning.move_appetite(thread, finding.move_kind or ""),
    }


@app.post("/threads/{thread_id}/corrections", response_model=CorrectionOut)
def add_correction(thread_id: str, body: CorrectionIn) -> CorrectionOut:
    """Tell this thread what the system got wrong.

    Reachable outside the reject sheet on purpose: a user can be wrong-footed
    by a finding, or by nothing at all, and still want to say "the choosing is
    mine here" before the next pass runs.
    """
    if not body.statement.strip():
        raise HTTPException(400, "a correction needs words")
    try:
        fact = threads_mod.correct(thread_id, body.statement)
    except threads_mod.ThreadError:
        raise HTTPException(404, f"no thread {thread_id}")
    return CorrectionOut(id=fact.id, statement=fact.statement, said_at=fact.updated_at)


@app.delete("/threads/{thread_id}/corrections/{fact_id}")
def drop_correction(thread_id: str, fact_id: str) -> Dict[str, object]:
    """Take one back. Soft-deleted, like every other fact the user retracts —
    that they said it and then unsaid it is itself worth keeping."""
    fact = db.get_fact(fact_id)
    if fact is None or fact.subject_id != thread_id:
        raise HTTPException(404, "no such correction on this thread")
    fact.status = "dismissed"
    db.upsert_fact(fact)
    return {"dropped": fact_id}


@app.post("/threads/{thread_id}/rework")
def rework_thread(thread_id: str) -> Dict[str, object]:
    """Work this thread again, now.

    The correction flow's payoff: the user watches their correction land
    instead of waiting for the next scheduled cycle. It costs one worker pass,
    which is worth spending on the rare occasion someone types a sentence to
    fix the system's understanding.
    """
    if db.get_thread(thread_id) is None:
        raise HTTPException(404, f"no thread {thread_id}")
    run = worker.work(thread_id)
    if run is None:
        raise HTTPException(503, db.get_sync_state("llm:last_error") or "no provider available")
    return {"thread_id": thread_id, "run_id": run.run_id, "conclusion": run.conclusion}


@app.post("/threads/{thread_id}/findings/{finding_id}/accept")
def accept_move(thread_id: str, finding_id: str) -> Dict[str, object]:
    """The user acted on a staged move. Recorded so appetite is not a ratchet —
    a signal that only ever falls would let initiative decay to nothing without
    anyone deciding it should."""
    thread = db.get_thread(thread_id)
    finding = db.get_finding(finding_id)
    if thread is None or finding is None:
        raise HTTPException(404, "no such thread or finding")

    learning.record_move("accepted", thread, finding.move_kind)
    return {
        "accepted": finding_id,
        "appetite": learning.move_appetite(thread, finding.move_kind or ""),
    }


@app.post("/threads/{thread_id}/contact", response_model=ThreadOut)
def set_thread_contact(thread_id: str, body: ContactIn) -> ThreadOut:
    """Who to write to when this thread needs a message. `person_id: null`
    clears it.

    Not "who the thread is about" — most threads are about no one. This is
    the counterpart the writer addresses, and it exists because a thread the
    user declares has no evidence to infer one from.
    """
    try:
        return _thread_out(threads_mod.set_contact(thread_id, body.person_id))
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)


@app.post("/threads/{thread_id}/deadline", response_model=ThreadOut)
def thread_deadline(thread_id: str, body: DeadlineIn) -> ThreadOut:
    """Set, override, or clear the deadline chip. A user date always wins; an
    inferred one must name the evidence that implied it."""
    try:
        thread = threads_mod.set_deadline(
            thread_id,
            body.date,
            source=body.source,
            evidence=body.evidence,
            reason=body.reason,
        )
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)
    return _thread_out(thread)


@app.post("/threads/{thread_id}/draft", response_model=ThreadDraftOut)
def draft_for_thread(thread_id: str) -> ThreadDraftOut:
    """Write a reply for this thread, **now**, with everything it knows.

    §v2 decision: drafts move to write-time. Extraction-time replies are
    written from one message with no idea what loop it belongs to, which is how
    33 items in this database ended up drafted "yep, I'll take care of it" —
    four of them addressed to billing robots.

    The writer is allowed to refuse. Plenty of threads are real work whose work
    is not a message: a bill gets paid, a booking gets checked. When it refuses
    it says what to do instead, and that is a better answer than a draft nobody
    can send.
    """
    try:
        brief = threads_mod.draft_brief(thread_id)
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)

    drafted: List[dict] = []
    tools = (
        list(assistant_registry.READ_TOOLS)
        + assistant_registry.draft_tools(drafted, thread=db.get_thread(thread_id))
        + assistant_registry.thread_tools()
    )
    run = assistant_loop.run_loop(
        "Draft the message for this thread, or explain why one doesn't make "
        f"sense.\n\n{_json.dumps(brief, indent=2, default=str)}",
        trigger="draft",
        tools=tools,
        system=assistant_tools.DRAFT_SYSTEM,
        max_iterations=6,
    )
    if run is None:
        raise HTTPException(503, "no tool-capable provider configured")

    return ThreadDraftOut(
        thread_id=thread_id,
        draft=DraftOut(**drafted[-1]) if drafted else None,
        reason=run.conclusion,
        trace=_trace(run.tool_calls),
    )


@app.delete("/threads/{thread_id}/watchers/{watcher_id}", response_model=ThreadDetailOut)
def stop_watcher(thread_id: str, watcher_id: str) -> ThreadDetailOut:
    """Retire a monitor. The user gets the last word on what the system keeps
    an eye on — an autonomous watch nobody can switch off is surveillance."""
    if not thread_watchers_mod.remove(watcher_id):
        raise HTTPException(404, f"no watcher {watcher_id}")
    return thread_detail(thread_id)


@app.post("/threads/{thread_id}/evidence", response_model=ThreadDetailOut)
def claim_evidence(thread_id: str, body: EvidenceIn) -> ThreadDetailOut:
    """Attach a row to a thread. The row is not consumed — one item can serve
    several threads."""
    try:
        threads_mod.claim(thread_id, body.ref_id, kind=body.kind, role=body.role, note=body.note)
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)
    return thread_detail(thread_id)


@app.delete("/threads/{thread_id}/evidence/{ref_id}", response_model=ThreadDetailOut)
def unclaim_evidence(thread_id: str, ref_id: str, kind: str = "item") -> ThreadDetailOut:
    if not threads_mod.unclaim(thread_id, ref_id, kind=kind):
        raise HTTPException(404, f"thread {thread_id} does not claim {kind} {ref_id}")
    return thread_detail(thread_id)


@app.post("/items/{item_id}/promote", response_model=ThreadDetailOut)
def promote_item(item_id: str, body: Optional[PromoteIn] = None) -> ThreadDetailOut:
    """Promote a surfaced card into a thread — the second capture verb the
    vision names. The item survives as the thread's founding evidence, and any
    date it carries becomes an inferred deadline with the item as its receipt."""
    body = body or PromoteIn()
    try:
        thread = threads_mod.promote_item(item_id, title=body.title, summary=body.summary)
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)
    return thread_detail(thread.id)


# ------------------------------------------------------------- proposals
@app.get("/proposals", response_model=List[ThreadOut])
def proposals() -> List[ThreadOut]:
    """Threads the system proposed, waiting in their own view. Visited by
    choice; never mixed into the stack."""
    return [_thread_out(t) for t in db.list_threads(states=[ThreadState.PROPOSED])]


@app.post("/proposals/{thread_id}/accept", response_model=ThreadOut)
def accept_proposal(thread_id: str) -> ThreadOut:
    try:
        return _thread_out(threads_mod.accept_proposal(thread_id))
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)


@app.post("/proposals/{thread_id}/dismiss", response_model=ThreadOut)
def dismiss_proposal(thread_id: str) -> ThreadOut:
    """Ignoring a proposal archives it rather than deleting it — the refusal is
    signal, the same way a dismissed fact is."""
    try:
        return _thread_out(threads_mod.dismiss_proposal(thread_id))
    except threads_mod.ThreadError as exc:
        raise _thread_error(exc)


# ------------------------------------------------------------------- sync
@app.get("/sync/changes")
def sync_changes(since: Optional[str] = None) -> Dict[str, Any]:
    """Delta for the on-device store."""
    items = db.list_items(updated_since=since) if since else db.list_items()
    return {
        "server_time": now_iso(),
        "items": [ItemOut.of(i).model_dump() for i in items],
        "confirmations": [c.model_dump() for c in _confirmations()],
    }


@app.post("/sync/poll")
def sync_poll(background: BackgroundTasks, wait: bool = False) -> Dict[str, Any]:
    """Force a poll cycle. `wait=true` blocks and returns the summary."""
    if wait:
        return poller.cycle()
    background.add_task(poller.cycle)
    return {"started": True}


# ------------------------------------------------------------- ingestion
@app.post("/ingest/import")
def ingest_import(body: ImportIn) -> Dict[str, Any]:
    """Import an iMessage or WhatsApp export (§9 — no live read API on iOS)."""
    path = Path(body.path).expanduser()
    if not path.exists():
        raise HTTPException(400, f"no file at {path}")
    resolver = IdentityResolver()
    if body.source == "imessage":
        count = imessage.import_export(path, resolver)
    elif body.source == "whatsapp":
        count = whatsapp.import_export(path, body.contact_name, body.is_group, resolver)
    else:
        raise HTTPException(400, "source must be 'imessage' or 'whatsapp'")
    created = pipeline.run()
    return {"messages_imported": count, "items_extracted": len(created)}


# §v3 workstream 5 — what a relayed knock resolves to. The relay carried
# only ids; the phone's notification extension calls here (bearer-authed,
# like everything) to fetch the words this engine queued, so the alert the
# user reads is written by their own machine and no other.
@app.get("/push/card")
def push_card(finding_id: Optional[str] = None, thread_id: Optional[str] = None) -> Dict[str, Any]:
    conn = db.get_connection()
    row = None
    if finding_id:
        row = conn.execute(
            "SELECT title, body, finding_id FROM notifications WHERE finding_id = ? "
            "ORDER BY created_at DESC LIMIT 1", (finding_id,)).fetchone()
    if row is None and thread_id:
        thread = db.get_thread(thread_id)
        if thread is not None:
            findings = db.thread_findings(thread_id)
            headline = findings[0].headline if findings else "Something moved here."
            return {"title": thread.title, "body": headline,
                    "thread_id": thread_id, "finding_id": None}
    if row is None:
        raise HTTPException(404, "nothing queued for those ids")
    finding = db.get_finding(row["finding_id"]) if row["finding_id"] else None
    return {"title": row["title"], "body": row["body"],
            "thread_id": finding.thread_id if finding else thread_id,
            "finding_id": row["finding_id"]}


# §v3 workstream 2 — the setup wizard. Thin wrappers over `wizard.py`; the
# auth gate already makes all of this loopback-only, which is exactly the
# wizard's audience: the person sitting at this Mac.
from . import wizard as setup_wizard  # noqa: E402


@app.get("/setup", response_class=HTMLResponse)
def setup_page() -> HTMLResponse:
    page = Path(__file__).parent / "setup_page.html"
    return HTMLResponse(page.read_text())


@app.get("/setup/status")
def setup_status() -> Dict[str, Any]:
    return setup_wizard.status()


@app.post("/setup/fda/open")
def setup_fda_open() -> Dict[str, bool]:
    setup_wizard.open_fda_settings()
    return {"opened": True}


@app.post("/setup/key")
def setup_key(body: Dict[str, str]) -> Dict[str, Any]:
    return setup_wizard.save_key(body.get("key", ""))


@app.post("/setup/pair")
def setup_pair() -> Dict[str, Any]:
    import io

    import qrcode
    import qrcode.image.svg

    from . import transport

    minted = api_auth.start_pairing()
    reach = setup_wizard._reachable_url()
    payload = _json.dumps({"v": 1, "url": reach, "code": minted["code"],
                           "urls": transport.urls()})
    image = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage,
                        box_size=12)
    buffer = io.BytesIO()
    image.save(buffer)
    return {"code": minted["code"], "expires_in_minutes": minted["expires_in_minutes"],
            "reach_url": reach, "qr_svg": buffer.getvalue().decode()}


# ---------------------------------------------------------------- pairing
# §v3 — how a phone earns its way in. `start` runs from a trusted context
# (the Mac, or an already-paired device adding another); `claim` is the one
# open door, spending a short-lived single-use code for a bearer token.
@app.post("/pair/start")
def pair_start() -> Dict[str, Any]:
    return api_auth.start_pairing()


@app.post("/pair/claim")
def pair_claim(body: PairClaimIn) -> Dict[str, Any]:
    minted = api_auth.claim_pairing(body.code, body.device_name)
    if minted is None:
        raise HTTPException(404, "that code is unknown, expired, or already used — mint a fresh one on the Mac")
    from . import transport

    # §v3 ws4 — the phone learns every door at the moment it earns a key,
    # so a DHCP move or leaving the house never strands it on one address.
    return {"token": minted["token"], "server_name": "Loose Ends engine",
            "urls": transport.urls()}


@app.get("/sources/notifications")
def notification_sources() -> Dict[str, Any]:
    """Which apps this engine has read notifications from, and how many.

    §v3 — the answer to "what is it actually reading?", which this source has
    to be able to give on demand: it samples every app's notifications, which
    is the most intrusive-*feeling* thing the engine does even though it never
    leaves the Mac. A number per app, and an off switch, beat a promise.
    """
    import os

    from ..ingestion import notifications as notif

    return {
        "readable": notif.readable(),
        "enabled": not os.environ.get("LIFELINE_NO_NOTIFICATIONS"),
        "apps": notif.seen_apps(),
    }


@app.get("/transport")
def transport_doors() -> Dict[str, Any]:
    """§v3 ws4 — the engine's current doors. Paired phones refresh this on
    sync, so candidates stay live as the network changes under the engine.
    Behind the gate like everything else."""
    from . import transport

    return transport.describe()


@app.get("/pair/status")
def pair_status(code: str) -> Dict[str, Any]:
    """For the wizard's screen — flips the moment the phone claims."""
    status = api_auth.pairing_status(code)
    if status is None:
        raise HTTPException(404, "no such pairing code")
    return status


@app.get("/auth/tokens")
def auth_tokens() -> List[Dict[str, Any]]:
    return api_auth.list_tokens()


@app.post("/auth/tokens/{token_id}/revoke")
def auth_token_revoke(token_id: str) -> Dict[str, Any]:
    if not api_auth.revoke_token(token_id):
        raise HTTPException(404, "no live token with that id")
    return {"status": "revoked"}


# ---------------------------------------------------------------- devices
@app.post("/devices")
def register_device(body: DeviceIn) -> Dict[str, Any]:
    db.register_device(body.token, body.platform)
    return {"registered": True, "devices": len(db.list_devices())}


# ------------------------------------------------------------------ model
@app.get("/model/weights")
def model_snapshot() -> Dict[str, Any]:
    """§8.3 transparency — everything the ranking engine has learned."""
    return {
        "weights": learning.snapshot(),
        "static_signal_weights": scorer.DEFAULT_WEIGHTS,
        "patterns": {
            "avoidance": [ItemOut.of(i).model_dump() for i in db.open_items() if i.behavior_pattern == "avoidance"],
            "deprioritized": [
                ItemOut.of(i).model_dump() for i in db.open_items() if i.behavior_pattern == "deprioritized"
            ],
        },
    }


# ------------------------------------------------------------------ admin
@app.post("/admin/purge")
def purge() -> Dict[str, Any]:
    """§12 — purge all extracted data and re-sync from scratch."""
    db.purge_all()
    return {"purged": True}


@app.post("/admin/load-sample")
def load_sample() -> Dict[str, Any]:
    from ..ingestion import load_sample_corpus

    counts = load_sample_corpus()
    created = pipeline.run()
    outcome = engine.scan()
    scheduler.run()
    return {"ingested": counts, "extracted": len(created), "completion": outcome.summary()}
