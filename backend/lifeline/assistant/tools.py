"""Read tools the assistant uses to build context on demand — the same
retrieval surface a future agentic tool-loop will call. Pure functions over the
store, no side effects: search the conversation, look a person up in the model,
check the calendar.
"""
from __future__ import annotations

import difflib
import json
import re as _re
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .. import db
from ..models import Item, parse_iso
from ..ranking import relationships

_STOP = {
    "the", "and", "you", "your", "for", "with", "this", "that", "have", "from",
    "about", "will", "would", "there", "their", "them", "just", "please", "when",
    "what", "were", "been", "http", "https", "com", "www", "sent", "reply", "message",
}


def _key_terms(item: Item, n: int = 4) -> List[str]:
    """The distinctive words that identify 'a message like this'."""
    parts = [item.entities.item or "", item.raw_text or ""]
    message = db.get_message(item.message_id) if item.message_id else None
    if message:
        parts.append(str(message.metadata.get("subject") or ""))
    words = re.findall(r"[a-z0-9']{4,}", " ".join(parts).lower())
    seen: List[str] = []
    for w in words:
        if w not in _STOP and w not in seen:
            seen.append(w)
    return seen[:n]


def similar_message_stats(item: Item) -> dict:
    """"How often have we received a message like this?" — the question the engine
    should ask before it surfaces something. Matches the item's distinctive terms
    across the whole store and reports the pattern: how many, over what span, how
    often, and whether you replied to the earlier ones."""
    terms = _key_terms(item)
    if len(terms) < 2:
        return {"count": 1, "recurring": False, "terms": terms}
    conn = db.get_connection()
    where = " AND ".join(["LOWER(text) LIKE ?"] * min(len(terms), 3))
    rows = conn.execute(
        f"SELECT conversation_id, timestamp FROM messages WHERE is_from_user = 0 AND {where} "
        f"ORDER BY timestamp",
        tuple(f"%{t}%" for t in terms[:3]),
    ).fetchall()
    count = len(rows)
    stats: dict = {"count": count, "recurring": count >= 2, "terms": terms[:3]}
    if count >= 2:
        first, last = parse_iso(rows[0]["timestamp"]), parse_iso(rows[-1]["timestamp"])
        if first and last:
            span = max(1.0, (last - first).total_seconds() / 86400.0)
            stats["span_days"] = round(span)
            stats["cadence_days"] = round(span / max(count - 1, 1))
        stats["last_seen"] = rows[-1]["timestamp"]
        # Did you reply to the earlier ones? (evidence you tend to engage)
        replied = sum(1 for r in rows[:-1] if db.user_reply_after(r["conversation_id"], r["timestamp"]))
        stats["you_replied_before"] = replied
    return stats


@dataclass
class Context:
    """What the tools gathered, ready to hand to the model (or summarise)."""
    messages: List[dict] = field(default_factory=list)
    people: List[dict] = field(default_factory=list)
    events: List[dict] = field(default_factory=list)

    def sources(self) -> List[str]:
        # Receipts must name something — an empty label ("calendar: ") is noise.
        out = [f"message from {m['sender']}" for m in self.messages[:5] if m.get("sender")]
        out += [f"person: {p['name']}" for p in self.people if p.get("name")]
        # `search_calendar` returns `summary`, not `title` (§v2 audit). Reading
        # the wrong key made every calendar source silently drop out of /ask
        # receipts, and raised KeyError in build_prompt — swallowed by
        # providers.run's bare except, so the RAG-lite fallback had never run.
        out += [f"calendar: {e['summary']}" for e in self.events[:3] if e.get("summary")]
        return out


def search_messages(
    query: Optional[str] = None,
    person_id: Optional[str] = None,
    limit: int = 8,
    source: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    direction: Optional[str] = None,
    has_link: bool = False,
) -> List[dict]:
    """Keyword search across the whole multi-source message store, newest first.

    v2 adds the filters that make it usable for the questions people actually
    ask: a date range ("what did I say last week"), a source, a direction, and
    links. Blunt keyword-only search is a top cause of shallow answers — and
    every result now carries its `message_id`, so a hit can be opened in full
    with `get_message` instead of being read through a 280-character window.
    """
    conn = db.get_connection()
    clauses: List[str] = []
    params: List[object] = []

    match = _fts_query(query)
    global _last_query_terms
    _last_query_terms = _re.findall(r"[A-Za-z0-9']+", query or "")
    if person_id:
        clauses.append("m.person_id = ?")
        params.append(person_id)
    if source:
        clauses.append("m.source = ?")
        params.append(source)
    if since:
        clauses.append("m.timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("m.timestamp <= ?")
        params.append(until)
    if direction == "from_you":
        clauses.append("m.is_from_user = 1")
    elif direction == "from_them":
        clauses.append("m.is_from_user = 0")
    if has_link:
        clauses.append("m.text LIKE '%http%'")
    if not match and not clauses:
        return []
    limit = max(1, min(limit, 40))
    where = (" AND " + " AND ".join(clauses)) if clauses else ""

    if not match:
        rows = conn.execute(
            f"SELECT m.id, m.source, m.conversation_id, m.person_id, m.is_from_user, "
            f"m.timestamp, m.text, m.metadata FROM messages m "
            f"WHERE 1=1{where} ORDER BY m.timestamp DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_message_hit(r) for r in rows]

    # Ranked MATCH instead of the LIKE '%term%' scan this replaced — which
    # had no word boundaries and no ordering, so a child's name returned a
    # term-life-insurance ad above her school's email. Two legs: the message's
    # own words, and the text of anything it carried (§v2.8 phase 0); a hit in
    # a PDF surfaces as the message that brought it.
    rows = conn.execute(
        f"""SELECT * FROM (
            SELECT m.id, m.source, m.conversation_id, m.person_id, m.is_from_user,
                   m.timestamp, m.text, m.metadata,
                   NULL AS via_attachment, bm25(messages_fts) AS rank
            FROM messages_fts
            JOIN messages m ON m.rowid = messages_fts.rowid
            WHERE messages_fts MATCH ?{where}
            UNION ALL
            SELECT m.id, m.source, m.conversation_id, m.person_id, m.is_from_user,
                   m.timestamp, m.text, m.metadata,
                   a.filename AS via_attachment, bm25(attachments_fts) AS rank
            FROM attachments_fts
            JOIN attachments a ON a.rowid = attachments_fts.rowid
            JOIN messages m ON m.id = a.message_id
            WHERE attachments_fts MATCH ?{where}
        ) GROUP BY id ORDER BY rank LIMIT ?""",
        (match, *params, match, *params, limit),
    ).fetchall()
    return [_message_hit(r) for r in rows]


def _fts_query(query: Optional[str]) -> Optional[str]:
    """A user's words as a safe FTS5 expression: bare terms OR'd, so partial
    vocabulary still matches and bm25 ranks the doc that has more of it."""
    terms = [t for t in _re.findall(r"[A-Za-z0-9']+", query or "") if len(t) > 1][:8]
    return " OR ".join(f'"{t}"' for t in terms) or None


def read_attachment(message_id: str, filename: Optional[str] = None,
                    offset: int = 0, limit: int = 6000) -> dict:
    """The text inside a file a message carried (§audit F1).

    The extraction pipeline parses PDFs and FTS indexes them, but until this
    tool existed nothing ever handed `attachments.text` to the model — it
    could learn a document *matched* and never quote it. The model said so
    itself, unprompted, four times in one audit.

    A scanned file returns its error verbatim ("no extractable text
    (scanned?)") — that is the honest answer, not a failure.
    """
    rows = db.attachments_for_message(message_id)
    if not rows:
        return {"error": f"message {message_id} carries no attachments"}
    if filename:
        rows = [a for a in rows if a.filename == filename] or rows
    out = []
    for attachment in rows[:3]:
        if attachment.text:
            chunk = attachment.text[offset:offset + max(200, min(limit, 12000))]
            out.append({
                "filename": attachment.filename,
                "mime": attachment.mime,
                "chars": len(attachment.text),
                "offset": offset,
                "text": chunk,
                "truncated": offset + len(chunk) < len(attachment.text),
            })
        else:
            out.append({
                "filename": attachment.filename,
                "mime": attachment.mime,
                "error": attachment.error or "no text extracted",
            })
    return {"message_id": message_id, "attachments": out}


def _attachment_excerpt(message_id: str, terms: List[str]) -> Optional[str]:
    """~280 chars of the attachment around the first search term that appears
    in it — so a search hit through a document shows the document's words,
    not the covering email's."""
    for attachment in db.attachments_for_message(message_id):
        text = attachment.text or ""
        lowered = text.lower()
        for term in terms:
            i = lowered.find(term.lower())
            if i >= 0:
                start = max(0, i - 100)
                return text[start:start + 280].strip()
    return None


def _message_hit(row) -> dict:
    person = db.get_person(row["person_id"]) if row["person_id"] else None
    meta = json.loads(row["metadata"] or "{}")
    hit = {
        "message_id": row["id"],
        "source": row["source"],
        "sender": "You" if row["is_from_user"] else (person.display_name if person else "someone"),
        "person_id": row["person_id"],
        "timestamp": row["timestamp"],
        "text": (row["text"] or "")[:280],
        "truncated": len(row["text"] or "") > 280,
    }
    if meta.get("subject"):
        hit["subject"] = meta["subject"]
    if meta.get("from_email"):
        hit["from_email"] = meta["from_email"]
    try:
        if row["via_attachment"]:
            hit["via_attachment"] = row["via_attachment"]
            excerpt = _attachment_excerpt(row["id"], _last_query_terms)
            if excerpt:
                hit["attachment_excerpt"] = excerpt
    except (IndexError, KeyError):
        pass          # callers that select without the attachment leg
    return hit


# The terms of the search currently being rendered — set by search_messages
# so _message_hit can excerpt the attachment around the match. Module-level
# because _message_hit serves several callers that have no query at all.
_last_query_terms: List[str] = []


def get_message(message_id: str) -> Optional[dict]:
    """One message, in **full**. Every other read truncates — `read_conversation`
    at 400 characters, search at 280 — which is exactly where a bill's amount,
    a due date, or an itinerary's flight number lives."""
    message = db.get_message(message_id)
    if not message:
        return None
    person = db.get_person(message.person_id) if message.person_id else None
    return {
        "message_id": message.id,
        "source": message.source,
        "sender": "You" if message.is_from_user else (person.display_name if person else "someone"),
        "person_id": message.person_id,
        "timestamp": message.timestamp,
        "subject": message.metadata.get("subject"),
        "from_email": message.metadata.get("from_email"),
        "labels": message.metadata.get("labels", []),
        "text": message.text,
    }


def search_mail(
    query: Optional[str] = None,
    sender: Optional[str] = None,
    label: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = 10,
) -> List[dict]:
    """Structured search over email metadata we already store and could not
    query: sender address or domain, Gmail labels, direction, date.

    This is how bills, confirmations and receipts are actually identified —
    by who sent them and how Gmail filed them, not by guessing keywords.
    `label` matches any Gmail label (`IMPORTANT`, `SENT`, `CATEGORY_PERSONAL`);
    `sender` matches an address or a bare domain.
    """
    conn = db.get_connection()
    clauses = ["source = 'gmail'"]
    params: List[object] = []

    for term in [t for t in (query or "").lower().split() if len(t) > 2][:6]:
        clauses.append("LOWER(text) LIKE ?")
        params.append(f"%{term}%")
    if sender:
        clauses.append("LOWER(json_extract(metadata, '$.from_email')) LIKE ?")
        params.append(f"%{sender.strip().lower()}%")
    if label:
        clauses.append("EXISTS (SELECT 1 FROM json_each(m.metadata, '$.labels') WHERE value = ?)")
        params.append(label.strip().upper())
    if since:
        clauses.append("m.timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("m.timestamp <= ?")
        params.append(until)
    if direction == "from_you":
        clauses.append("m.is_from_user = 1")
    elif direction == "from_them":
        clauses.append("m.is_from_user = 0")

    rows = conn.execute(
        f"SELECT id, source, conversation_id, person_id, is_from_user, timestamp, text, metadata "
        f"FROM messages m WHERE {' AND '.join(clauses)} ORDER BY m.timestamp DESC LIMIT ?",
        (*params, max(1, min(limit, 40))),
    ).fetchall()
    return [_message_hit(r) for r in rows]


def timeline(
    person_id: Optional[str] = None,
    query: Optional[str] = None,
    days: int = 90,
    limit: int = 30,
) -> List[dict]:
    """One person or one topic across **every** channel, oldest last.

    The cross-channel view the whole thesis rests on: an iMessage thread, the
    emails, the calendar entries and the items extracted from them, interleaved
    in the order they actually happened. Answering "what's going on with X"
    from one source at a time is how the loop produces half a picture.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat(timespec="seconds")
    entries: List[dict] = []

    for hit in search_messages(query=query, person_id=person_id, since=cutoff, limit=limit):
        entries.append({
            "at": hit["timestamp"],
            "kind": "message",
            "source": hit["source"],
            "who": hit["sender"],
            "what": hit.get("subject") or hit["text"][:160],
            "message_id": hit["message_id"],
        })

    for item in db.list_items(person_id=person_id):
        if item.timestamp < cutoff:
            continue
        if query and query.lower() not in f"{item.raw_text} {item.suggested_action}".lower():
            continue
        entries.append({
            "at": item.timestamp,
            "kind": "item",
            "source": item.source,
            "who": item.person,
            "what": item.entities.item or item.suggested_action or item.raw_text[:160],
            "status": item.status,
        })

    needle = (query or "").strip().lower()
    for event in db.list_calendar_events():
        if not event.start_at or event.start_at < cutoff:
            continue
        if needle and needle not in f"{event.summary} {event.description} {event.location}".lower():
            continue
        entries.append({
            "at": event.start_at,
            "kind": "calendar",
            "source": "calendar",
            "who": None,
            "what": event.summary,
        })

    entries.sort(key=lambda e: e["at"])
    return entries[-limit:]


def search_history(
    query: Optional[str] = None,
    person_id: Optional[str] = None,
    limit: int = 10,
) -> List[dict]:
    """Closed items and how they closed — precedent, rather than guessing.

    "Have I dealt with this before, and how?" is answerable: 147 of this
    database's items are completed, about half of them automatically, each with
    the evidence that closed it. That is the single richest signal about how
    the user actually handles things, and nothing could read it.
    """
    items = [i for i in db.list_items(person_id=person_id) if i.status in ("completed", "dismissed")]
    needle = (query or "").strip().lower()
    if needle:
        items = [
            i for i in items
            if needle in f"{i.raw_text} {i.suggested_action} {i.entities.item or ''}".lower()
        ]
    items.sort(key=lambda i: i.completed_at or i.updated_at, reverse=True)

    out = []
    for item in items[:limit]:
        signals = [s for s in db.signals_for_item(item.id) if s.resolution in ("auto_closed", "confirmed")]
        out.append({
            "item_id": item.id,
            "person": item.person,
            "type": item.type,
            "what": item.entities.item or item.suggested_action or item.raw_text[:120],
            "status": item.status,
            "closed_at": item.completed_at,
            "closed_by": item.completed_by or ("manual" if item.status == "completed" else None),
            "closed_because": "; ".join(signals[0].reasons) if signals else None,
        })
    return out


def _normalise_handle(handle: str) -> str:
    """Phone numbers and emails as they'd match, not as they're stored.
    `+1 (646) 555-0149`, `6465550149` and `646-555-0149` are one person."""
    text = (handle or "").strip().lower()
    if "@" in text:
        return text
    digits = re.sub(r"\D", "", text)
    # US numbers arrive with and without the country code; compare the last 10.
    return digits[-10:] if len(digits) >= 10 else digits


def _name_score(needle: str, person) -> float:
    """How well a typed name matches a person, 0-1.

    Built from the actual misses in `loop_runs`: "booooby" never resolved to
    "Robbbbie 😛👅 Carter" (one letter out, plus emoji in the display name),
    and "Nia" never resolved to "Nia Coleman" — both because the old
    matcher was a plain substring scan and a shortest-name tie-break.
    """
    display = (person.display_name or "").lower()
    if not display:
        return 0.0

    # An exact handle is proof, not a guess.
    normalised = _normalise_handle(needle)
    if normalised and any(_normalise_handle(h) == normalised for h in person.handles or []):
        return 1.0
    if needle == display:
        return 1.0

    # Compare against name tokens with the decoration stripped — emoji and
    # punctuation are display, not identity.
    tokens = [t for t in re.findall(r"[a-z0-9']+", display) if t]
    if not tokens:
        return 0.0
    if needle in tokens:
        return 0.95
    if needle in display:
        return 0.85
    best = 0.0
    for token in tokens:
        if token.startswith(needle) or needle.startswith(token):
            best = max(best, 0.8)
        best = max(best, difflib.SequenceMatcher(None, needle, token).ratio())
    # Whole-name similarity catches "katie bishop" typed in full.
    best = max(best, difflib.SequenceMatcher(None, needle, " ".join(tokens)).ratio())
    return best


FIND_PERSON_FLOOR = 0.72     # below this a "match" is noise, not a near-miss


def find_person(name: str) -> Optional[dict]:
    """Resolve a person by name or handle and return their model card.

    Returns `alternatives` whenever the field is close — the database has both
    a Katie Bishop and a Katie Marsh, and answering "Katie" with one of them
    silently is how a draft goes to the wrong person. Naming both lets the
    model ask which, instead of guessing or (as it did) asking who "her" is.
    """
    needle = (name or "").strip().lower()
    if not needle:
        return None

    scored = [(p, _name_score(needle, p)) for p in db.list_people()]
    scored = [(p, s) for p, s in scored if s >= FIND_PERSON_FLOOR]
    if not scored:
        return None
    # Best score first; on a tie the shorter name is the more specific match.
    scored.sort(key=lambda ps: (-ps[1], len(ps[0].display_name or "")))
    best, score = scored[0]

    open_items = [i for i in db.list_items(person_id=best.id) if i.status in ("pending", "snoozed")]
    card = {
        "person_id": best.id,
        "name": best.display_name,
        "relationship": best.relationship,
        "handles": best.handles,
        "tie_strength": relationships.strength(best.id),
        "tie": relationships.describe(best.id),
        "match_confidence": round(score, 2),
        "open_count": len(open_items),
        "open_items": [i.suggested_action or i.raw_text[:60] for i in open_items[:5]],
    }
    others = [
        {"person_id": p.id, "name": p.display_name, "match_confidence": round(s, 2)}
        for p, s in scored[1:4]
    ]
    if others:
        card["alternatives"] = others
        card["ambiguous"] = len(scored) > 1 and scored[1][1] >= score - 0.05
    return card


def search_calendar(
    query: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[dict]:
    """Calendar events. Upcoming by default; pass `since` to reach into the past.

    The default stayed upcoming-only through v1.5, and a real run asked "what
    appointments did I have yesterday" and was told "I don't have access to
    historical calendar data" — while fourteen past events sat in the table.
    The tool couldn't see them, so the model concluded the data didn't exist.
    A window it can widen is the difference between a limit and a lie.

    A keyword filters across summary, description and location; when nothing
    matches, every event in the window comes back instead — an empty match must
    read as 'look for yourself', never as 'calendar empty'.
    """
    from datetime import datetime, timezone

    lower = since or datetime.now(timezone.utc).date().isoformat()
    in_window = [
        e for e in db.list_calendar_events()
        if (e.start_at or "") >= lower
        and (not until or (e.start_at or "") <= until)
        and e.status != "cancelled"
    ]
    q = (query or "").strip().lower()
    matched = [
        e for e in in_window
        if q and q in f"{e.summary} {e.description} {e.location}".lower()
    ]
    events = matched if matched else in_window
    return [
        {
            "summary": e.summary,
            "start": e.start_at,
            "end": e.end_at,
            "location": e.location or None,
            "keyword_matched": bool(matched),
        }
        for e in events[:10]
    ]


def _mentioned_people(question: str) -> List[dict]:
    """People whose name appears in the question (first-name match is enough)."""
    words = {w.strip(",.?!").lower() for w in question.split() if len(w) > 2}
    found: Dict[str, dict] = {}
    for person in db.list_people():
        first = (person.display_name or "").split()[0].lower() if person.display_name else ""
        if first and first in words:
            card = find_person(person.display_name)
            if card:
                found[card["person_id"]] = card
    return list(found.values())


def gather_context(question: str) -> Context:
    """RAG-lite: run the read tools most relevant to the question and collect
    what they return. (A future version lets the model drive which tools fire.)"""
    people = _mentioned_people(question)
    person_id = people[0]["person_id"] if people else None
    return Context(
        messages=search_messages(question, person_id=person_id),
        people=people,
        events=search_calendar(),
    )


ASSISTANT_LOOP_SYSTEM = (
    "You are the user's personal assistant, with tools over their messages, "
    "people, calendar, and open items. Investigate before answering: call the "
    "tools you need, then conclude. Be concise and specific — name people, "
    "dates, and items.\n"
    "Any 'already known' facts at the top of the question are orientation, "
    "never the whole answer: use them as search terms, then verify and extend "
    "with the tools. There is no separate 'knowledge base' — the messages, "
    "mail and calendar ARE what you know, and you search them yourself.\n"
    "Never describe a search you could run ('I'd need to check your "
    "messages') and never offer to search — run it, this turn. Only after "
    "the tools come back empty may you say something isn't there, and then "
    "say what you searched.\n"
    "Messages carry files — bills, forms, minutes, statements. A search hit "
    "with `via_attachment` matched inside a document: call read_attachment "
    "with that message_id to read it. Amounts, account numbers, requirements "
    "and dates usually live in the file, not the covering email. If "
    "read_attachment returns an error like 'scanned', say the document "
    "exists but is unreadable — never answer from the email's teaser as if "
    "it were the document.\n"
    "Never invent facts. Your final message is shown to the user directly: "
    "plain text, no JSON, no preamble."
)

CONVERSE_SYSTEM = (
    "You are the user's personal aide — the voice of their Lifeline. You have "
    "tools over their messages, people, calendar, open items, and the facts "
    "they've told you.\n"
    "Decide what the user's message is:\n"
    "- A STATEMENT about their life ('Katie is the recruiter for the job I "
    "want'): save each durable fact with record_fact (resolve people with "
    "find_person; check get_facts for duplicates), then confirm in one short "
    "sentence.\n"
    "- A QUESTION ('what do I owe Katie?'): investigate with the read tools "
    "until you can answer with specifics — names, dates, counts.\n"
    "- Mixed: do both.\n"
    "How to investigate:\n"
    "- NEVER ask permission to use a tool. 'Would you like me to check your "
    "messages?' is a failure — check them.\n"
    "- NEVER say you don't have something, don't recognise a name, or can't "
    "see a date until you have called a tool and it came back empty. You have "
    "the user's whole world; an answer that guesses at what's in it will be "
    "wrong and will read as the system not knowing them.\n"
    "- A name you don't recognise: find_person. It matches nicknames, "
    "misspellings, emails and phone numbers. If it returns `alternatives`, "
    "several people match — name them and ask which. Never ask who someone is.\n"
    "- An empty result is a lead, not an answer. Try other words, then check "
    "the adjacent sources before concluding. Trips live in the calendar AND "
    "in emails; appointments live in the calendar AND in confirmation texts.\n"
    "- search_messages matches words that APPEAR IN messages. Search 'flight "
    "confirmation', never a description of the situation like 'sent message "
    "no response' — for that, use quiet_conversations. It also filters by "
    "person, source, date range, direction and links, and returns a "
    "`message_id` you can open in full with get_message.\n"
    "- Bills, statements, confirmations, receipts and anything from a company: "
    "search_mail by sender or domain, not by guessing keywords.\n"
    "- Amounts, due dates, flight numbers and addresses are usually past the "
    "truncation in a search result. Open the message with get_message before "
    "quoting a specific.\n"
    "- 'What's going on with X' or 'catch me up': timeline, which interleaves "
    "every channel in the order things happened.\n"
    "- 'Have I dealt with this before': search_history, which shows what closed "
    "and how.\n"
    "- Only conclude 'nothing found' after the plausible sources disagree "
    "with the question, and say which you checked.\n"
    "- Asked to WRITE, DRAFT, or COMPOSE a message: resolve the person with "
    "find_person, read_conversation to see what was actually said, then write it "
    "yourself and hand it over with draft_message. Do not ask what to say or "
    "what tone to use — the thread tells you both. Ask only if the thread is "
    "genuinely empty. Your reply then just says what you drafted and why.\n"
    "Voice: a consummate butler — courteous, precise, quietly warm. At most "
    "one dry aside per reply, and only when the facts earn it. Never "
    "obsequious, never wordy. Never invent facts.\n"
    "Your final message is shown to the user directly: plain text, no JSON, "
    "no preamble, 1-3 sentences."
)

DRAFT_SYSTEM = (
    "You write a message the user can send in one tap, for one open thread.\n"
    "You are given the thread, its evidence, and who it concerns. Read the "
    "actual conversation with read_conversation before writing — a reply that "
    "doesn't reference what was said is worse than none.\n"
    "Write in the user's voice as the thread shows it: brief, natural, no "
    "salutations or sign-offs the conversation wouldn't use. Then hand it over "
    "with draft_message.\n"
    "**Refuse when a reply makes no sense.** Much of what lands here is from a "
    "company, a no-reply address, or an automated notification — a bill, a "
    "statement, a booking confirmation. The thread is real work, but the work "
    "is paying the bill or checking the booking, not writing back to a robot. "
    "In that case call NO tool and say in one sentence what the user should "
    "actually do instead.\n"
    "Never invent facts, amounts, dates or commitments. If the thread doesn't "
    "say it, don't write it."
)

TELL_SYSTEM = (
    "The user is telling you something about their life so you can serve them "
    "better. Extract the durable facts and save each with record_fact — one "
    "call per distinct fact. When a fact is about a person, resolve them with "
    "find_person and use their person_id; check get_facts to avoid duplicates "
    "and to spot contradictions (record the new fact anyway — newest wins). "
    "Capture intent and priorities ('wants the job', 'low priority now'), not "
    "transient chatter. Then conclude with one short plain-text sentence "
    "confirming what you captured — no JSON, no lists, no preamble."
)

ASSISTANT_SYSTEM = (
    "You are the user's personal assistant. You can see their messages, the "
    "people they talk to, and their calendar. Answer the question concisely and "
    "specifically, using ONLY the provided context. Name people and specifics. "
    "If the context doesn't contain the answer, say what you'd need to check. "
    "Never invent facts. Return JSON only: {\"answer\": \"...\"}."
)


def build_prompt(question: str, ctx: Context) -> str:
    lines = [f"Question: {question}", ""]
    if ctx.people:
        lines.append("People:")
        for p in ctx.people:
            owe = "; ".join(p["open_items"]) or "nothing open"
            lines.append(f"- {p['name']} ({p['tie']}); you owe: {owe}")
        lines.append("")
    if ctx.messages:
        lines.append("Relevant messages (newest first):")
        for m in ctx.messages:
            lines.append(f"- [{m['timestamp'][:10]}] {m['sender']}: {m['text']}")
        lines.append("")
    if ctx.events:
        lines.append("Calendar: " + "; ".join(e["summary"] for e in ctx.events[:5]))
    return "\n".join(lines)


def fallback_answer(question: str, ctx: Context) -> str:
    """A useful answer without an LLM — just report what the tools found."""
    if ctx.people:
        p = ctx.people[0]
        if p["open_items"]:
            return f"You owe {p['name']} {p['open_count']}: " + "; ".join(p["open_items"]) + "."
        return f"Nothing open with {p['name']} right now."
    if ctx.messages:
        m = ctx.messages[0]
        return f"Most recent related message — {m['sender']}: “{m['text'][:160]}”."
    return "I couldn't find anything about that in your messages, people, or calendar."
