"""Extraction pipeline (§5, milestone 2).

Incremental by construction: only messages with ``extracted_at IS NULL`` are
ever sent to the classifier, so a re-run costs nothing and full history is
never re-processed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .. import db
from ..config import get_config
from ..models import Entities, Item, Message, new_id, now_iso, parse_iso
from . import dates, heuristic, providers

log = logging.getLogger(__name__)

BATCH_SIZE = 12
CONTEXT_MESSAGES = 4
# How much attachment text one message may add to its batch entry. The full
# text stays in the attachments table for retrieval; this is the classifier's
# ration, sized so one statement cannot crowd out the other eleven messages.
ATTACHMENT_TEXT_BUDGET = 4_000
MIN_CONFIDENCE = 0.35
VALID_TYPES = {"purchase", "event", "promise", "followup", "reading", "question"}


def _thread_context(message: Message, limit: int = CONTEXT_MESSAGES) -> List[Dict[str, str]]:
    """A few preceding turns, so "that bag" and "the paper" resolve."""
    history = db.thread_messages(message.conversation_id)
    prior = [m for m in history if m.timestamp < message.timestamp][-limit:]
    return [{"who": "user" if m.is_from_user else (m.person_id or "them"), "text": m.text[:280]} for m in prior]


def _person_name(message: Message) -> str:
    if message.person_id:
        person = db.get_person(message.person_id)
        if person:
            return person.display_name
    thread = next((t for t in db.list_conversations() if t.id == message.conversation_id), None)
    return thread.display_name if thread else "Unknown"


def _with_cargo(message: Message) -> str:
    """The message text plus what it carried (§v2.8 phase 0.4).

    The AutoPay-failure letters say "AutoPay Failure" in the PDF and nothing
    in the body; classified on the body alone they are just another no-reply
    email. Each file is named so the model can cite it, and the total is
    bounded so a 20k-character statement cannot crowd out the rest of the
    batch — the full text stays in the attachments table for retrieval.
    """
    if not message.metadata.get("attachments"):
        return message.text
    parts = [message.text]
    budget = ATTACHMENT_TEXT_BUDGET
    for attachment in db.attachments_for_message(message.id):
        if not attachment.text:
            continue
        if budget <= 0:
            parts.append("[more attachments omitted]")
            break
        chunk = attachment.text[:budget]
        budget -= len(chunk)
        parts.append(f"[attachment: {attachment.filename}]\n{chunk}")
    return "\n\n".join(parts)


def build_batch(messages: Sequence[Message]) -> List[Dict[str, Any]]:
    batch = []
    for m in messages:
        batch.append(
            {
                "id": m.id,
                "source": m.source,
                "person": _person_name(m),
                "timestamp": m.timestamp,
                "text": _with_cargo(m),
                "metadata": m.metadata,
                "thread_context": _thread_context(m),
            }
        )
    return batch


def _classify(batch: List[Dict[str, Any]]) -> "Tuple[Dict[str, Any], str]":
    """The classification and *who made it* — "llm" or "heuristic".

    The distinction is audit finding #4: when the budget ran out or every
    provider failed, the heuristic answered and the messages were stamped
    extracted forever, with nothing anywhere recording that the deliberately
    conservative rules — not a model — had made the call. An open loop
    silently dropped, on the days the system is most stressed.
    """
    cfg = get_config()
    result = providers.run(
        lambda p: p.classify_batch(batch, draft_replies=cfg.draft_replies), "classification"
    )
    if result is not None:
        return result, "llm"
    return heuristic.classify_batch(batch, draft_replies=cfg.draft_replies), "heuristic"


# §v2.8 phase 3 — how much world a single message may assert. Sprawl guard:
# a newsletter that name-drops thirty organisations is not thirty facts.
MAX_CLAIMS_PER_MESSAGE = 5
MAX_FACTS_PER_CLAIM = 4


def _absorb_claims(raw_claims: List[Dict[str, Any]], by_id: Dict[str, Any]) -> int:
    """Entity claims into the world model, with the message as receipt.

    Never raises — a malformed claim is dropped, per the same law that keeps
    one bad item from killing a batch. And it is deliberately choosier than
    the item path: a claim from promotional mail or a bulk sender is refused
    outright, because "every newsletter sender is technically an organisation"
    is how a world model becomes a mailing list.
    """
    from .. import world
    from ..ranking import relationships

    written = 0
    per_message: Dict[str, int] = {}
    for raw in raw_claims or []:
        try:
            message_id = raw.get("message_id")
            if message_id not in by_id:
                continue
            message, _ = by_id[message_id]
            if message.metadata.get("promotional") or message.metadata.get("list_unsubscribe"):
                continue
            if message.person_id and relationships._is_service(message.person_id):
                continue
            if per_message.get(message.id, 0) >= MAX_CLAIMS_PER_MESSAGE:
                continue

            name = str(raw.get("name") or "").strip()
            kind = str(raw.get("kind") or "").strip().lower()
            if not name or kind not in world.KINDS:
                continue
            try:
                confidence = min(1.0, max(0.0, float(raw.get("confidence", 0.7))))
            except (TypeError, ValueError):
                confidence = 0.7

            claims = raw.get("claims")
            if not isinstance(claims, list):
                claims = []
            entity = None
            for claim in claims[:MAX_FACTS_PER_CLAIM]:
                if not isinstance(claim, dict):
                    continue
                predicate = str(claim.get("predicate") or "").strip().lower()
                value = str(claim.get("value") or "").strip()
                if not predicate or not value:
                    continue
                # Relations pass through the closed vocabulary (audit F2/F6):
                # 43 free-text shapes for one predicate, "visited user's home"
                # filed as a relationship. Unmappable claims are not relations.
                if predicate == "relation_to_user":
                    canonical = world.canonical_relation(value)
                    if canonical is None:
                        continue
                    value = canonical
                if entity is None:
                    entity = world.upsert(kind, name)
                    per_message[message.id] = per_message.get(message.id, 0) + 1
                world.record_fact(entity.id, predicate, value,
                                  message_id=message.id, confidence=confidence)
                written += 1
        except Exception:
            log.exception("dropping malformed entity claim")
    return written


def _normalise_date(raw: Dict[str, Any], message: Message, item_type: str = "") -> Optional[str]:
    """Relative phrase -> absolute ISO, anchored on the message timestamp."""
    entities = raw.get("entities") or {}
    anchor = parse_iso(message.timestamp)
    phrase = entities.get("date_phrase")
    if phrase:
        resolved = dates.normalise(str(phrase), anchor)
        if resolved:
            return resolved
    # The model may have returned an already-absolute date.
    explicit = entities.get("date")
    if explicit:
        resolved = dates.normalise(str(explicit), anchor) or (str(explicit) if parse_iso(str(explicit)) else None)
        if resolved:
            return resolved
    # Last resort: scan the message itself. Loose markers ("today", a bare
    # weekday) only count when the sender framed something as a deadline, or
    # when the item is an event — where the date reference *is* the point.
    # Otherwise "saw it in the window today" becomes a due date.
    weak = item_type == "event" or dates.has_deadline_language(message.text)
    return dates.normalise(message.text, anchor, weak=weak)


def _link_followup(item: Item) -> Optional[str]:
    """§5: link a follow-up back to the earlier item it refers to."""
    if item.type != "followup" or not item.person_id:
        return None
    candidates = [
        c
        for c in db.list_items(person_id=item.person_id)
        if c.id != item.id and c.status in ("pending", "snoozed") and c.timestamp < item.timestamp
    ]
    if not candidates:
        return None

    # Cheap lexical pass first — it resolves the common case for free.
    words = {w for w in item.raw_text.lower().split() if len(w) > 4}
    best, best_overlap = None, 0
    for candidate in candidates:
        haystack = f"{candidate.raw_text} {candidate.entities.item or ''}".lower()
        overlap = sum(1 for w in words if w in haystack)
        if overlap > best_overlap:
            best, best_overlap = candidate, overlap
    if best and best_overlap >= 2:
        return best.id

    payload = [{"id": c.id, "type": c.type, "raw_text": c.raw_text, "timestamp": c.timestamp} for c in candidates[:20]]
    link = providers.run(
        lambda p: p.link_followup(item.to_spec_dict(), payload), "follow-up linking"
    )
    # The model answers with an id, and models invent ids. An invented one
    # used to reach save_item and die on the foreign key — killing the whole
    # cycle (audit finding #5). An id it was never offered is no link.
    return link if link in {c.id for c in candidates} else None


def _to_item(raw: Dict[str, Any], message: Message, person_name: str) -> Optional[Item]:
    item_type = str(raw.get("type", "")).strip().lower()
    if item_type not in VALID_TYPES:
        log.debug("dropping item with unknown type %r", item_type)
        return None
    # Coerce, never trust (audit finding #5): a string confidence raised out
    # of the whole batch, and a *missing* one defaulted to 1.0 — sailing past
    # MIN_CONFIDENCE on the model's silence. No confidence is no item.
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        log.debug("dropping item with unusable confidence %r", raw.get("confidence"))
        return None
    if confidence < MIN_CONFIDENCE:
        return None

    entities = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
    item = Item(
        id=new_id(),
        source=message.source,
        conversation_id=message.conversation_id,
        message_id=message.id,
        person_id=message.person_id,
        person=person_name,
        timestamp=message.timestamp,
        type=item_type,
        raw_text=message.text[:2000],
        entities=Entities(
            item=(entities.get("item") or None),
            date=_normalise_date(raw, message, item_type),
            link=(entities.get("link") or None),
        ),
        suggested_action=str(raw.get("suggested_action") or "").strip(),
        suggested_reply=(raw.get("suggested_reply")
                         if isinstance(raw.get("suggested_reply"), str) else None),
        status="pending",
        created_at=now_iso(),
    )
    return item


def world_backfill(days: int = 90, limit: int = 400) -> Dict[str, int]:
    """One-time claims pass over messages already extracted (§v2.8 phase 3).

    The ongoing path reads new messages once and gets both outputs from the
    one call. History was read before the second output existed, and re-opening
    it through `run()` would churn items (the 0.4 re-extraction produced 21
    already-past events). This pass sends the same batches through the same
    classifier and absorbs *only* the claims — items in the response are
    ignored, nothing is marked, nothing is re-scored.

    Deliberate and ceilinged: `limit` caps messages per invocation, newest
    first, promotional and service mail excluded before a token is spent.
    """
    from datetime import datetime, timedelta, timezone

    from ..ranking import relationships

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = db.get_connection().execute(
        """SELECT id FROM messages
           WHERE timestamp >= ? AND text != ''
             AND metadata NOT LIKE '%"promotional": true%'
             AND metadata NOT LIKE '%"list_unsubscribe": true%'
           ORDER BY timestamp DESC LIMIT ?""",
        (since, limit),
    ).fetchall()
    messages = [m for m in (db.get_message(r["id"]) for r in rows) if m]
    messages = [m for m in messages
                if not (m.person_id and relationships._is_service(m.person_id))]

    written = 0
    for start in range(0, len(messages), BATCH_SIZE):
        chunk = messages[start : start + BATCH_SIZE]
        batch = build_batch(chunk)
        by_id = {entry["id"]: (msg, entry["person"]) for entry, msg in zip(batch, chunk)}
        result = _classify(batch)
        written += _absorb_claims(result.get("entities") or [], by_id)
    return {"messages": len(messages), "facts_written": written}


def run(limit: int = 200, rescore: bool = True) -> List[Item]:
    """Process every un-extracted message. Returns the items created."""
    from ..ranking import scorer  # local import keeps the layering acyclic

    pending = db.unextracted_messages(limit=limit)
    if not pending:
        return []

    # The user's own messages are context, not sources of obligations.
    candidates = [m for m in pending if not m.is_from_user and m.text.strip()]
    created: List[Item] = []

    for start in range(0, len(candidates), BATCH_SIZE):
        chunk = candidates[start : start + BATCH_SIZE]
        batch = build_batch(chunk)
        by_id = {entry["id"]: (msg, entry["person"]) for entry, msg in zip(batch, chunk)}

        result, classified_by = _classify(batch)
        _absorb_claims(result.get("entities") or [], by_id)
        for raw in result.get("items") or []:
            message_id = raw.get("message_id")
            if message_id not in by_id:
                log.debug("classifier returned an unknown message_id %r", message_id)
                continue
            message, person_name = by_id[message_id]
            if db.find_item_by_message(message.id):
                continue    # already extracted in an earlier partial run
            # One malformed element must cost one item, never the batch: an
            # uncaught raise here re-sent (and re-billed) the same batch every
            # cycle while extracting nothing (audit finding #5).
            try:
                item = _to_item(raw, message, person_name)
                if not item:
                    continue
                db.save_item(item)
                link = _link_followup(item)
                if link:
                    item.links_to_item_id = link
                    db.save_item(item)
            except Exception:
                log.exception("dropping malformed classifier item for %s", message_id)
                continue
            created.append(item)

        # A batch the rules answered stays open for the model. The heuristic's
        # items are real (deduped on the re-run by find_item_by_message), but
        # extracted_at is a promise that a model read the message — unless no
        # model is configured at all, in which case the rules ARE the reader.
        if classified_by == "llm" or not providers.available():
            db.mark_extracted([m.id for m in chunk])
        else:
            log.warning("batch of %d classified by heuristic; left for the model "
                        "to re-read when providers recover", len(chunk))

    # The user's own messages and empty-text rows never reach a classifier;
    # mark them so they are not re-fetched every cycle. Candidates were marked
    # per-chunk above, by whoever actually read them.
    candidate_ids = {m.id for m in candidates}
    db.mark_extracted([m.id for m in pending if m.id not in candidate_ids])

    if rescore and created:
        scorer.rescore_all()
        # rescore_all() scores the rows it re-reads from the database, so every
        # object built above is stale the moment it returns — score 0.0, no
        # interruption level. Re-read rather than scoring here as well: one
        # scoring path, not two that can drift.
        created = [db.get_item(item.id) or item for item in created]
    return created
