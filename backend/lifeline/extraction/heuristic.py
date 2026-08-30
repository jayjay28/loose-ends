"""Deterministic fallback classifier.

Claude does the real classification (§5). This exists so that milestones 4-9
remain independently testable with no API key and no network, and so the
pipeline degrades to *something* rather than nothing if the API is down. It is
a rule cascade, and it is deliberately conservative: when in doubt, emit
nothing.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_URL = re.compile(r"https?://\S+")

# Ordered cascade: the first pattern that fires wins.
_SELF_PROMISE = re.compile(r"\b(i'?ll|i will|i promised|i'?m going to|i said i'?d)\b", re.I)
_REQUEST = re.compile(
    r"\b(can you|could you|would you mind|will you|please|need you to|you need to|"
    r"we should|we need to|don'?t forget|remember to|let me know|make sure you|"
    r"you have to|mind (?:grabbing|picking|bringing))\b",
    re.I,
)
_FOLLOWUP = re.compile(
    r"\b(did you (?:ever|get a chance|look|read|see|manage)|have you (?:had a chance|gotten|looked|read)|"
    r"any update|still waiting|following up|circling back|i'?ve asked|asked twice|"
    r"did you ever|any word on|what happened with)\b",
    re.I,
)
_READING = re.compile(
    r"\b(article|paper|essay|book|podcast|documentary|newsletter|piece|thread|link|"
    r"blog post|you'?d love (?:it|this)|worth a read|sending you)\b",
    re.I,
)
_PURCHASE = re.compile(
    r"\b(i want|want that|we want|buy|order (?:the|a|one)|purchase|pick up|grab (?:the|a|some)|"
    r"get (?:the|a|one|him|her|them)|shopping for|if you want it|need to get)\b",
    re.I,
)
_EVENT_NOUN = re.compile(
    r"\b(\d{1,3}(?:st|nd|rd|th)|birthday|anniversary|wedding|dinner|lunch|brunch|party|"
    r"trip|flight|ride|race|appointment|reservation|rsvp|graduation|funeral|visit|"
    r"sit-?down|show|game|recital|ceremony)\b",
    re.I,
)
_QUESTION = re.compile(
    r"(\?|^\s*(what|when|where|who|why|how|can|could|should|do|does|did|is|are|will|would|any)\b|"
    r"\bwant me to\b|\bare you (?:free|around|up for)\b)",
    re.I,
)
# A bare deadline statement: "the deadline is in 3 days", "registration closes
# Friday". No request verb and no event noun, so nothing above this catches it —
# but the sender has framed a date as something that runs out, which §5 treats
# as an `event` ("Grandma's 80th is in 3 weeks"). The only difference here is
# that the noun isn't a party. Requires a resolvable date reference too, so a
# standalone "the deadline slipped" stays silent.
_DEADLINE_NOUN = re.compile(
    r"\b(deadline|due date|cut-?off|last day|expires?|closes?)\b",
    re.I,
)

# Tone cues — consumed by the ranking engine (§6.1 Passive is partly explicit).
_FLEXIBLE = re.compile(
    r"\b(no rush|whenever|no pressure|totally whenever|if you (?:get|have) a (?:sec|chance|minute)|"
    r"sometime|at some point|eventually|when you can|no hurry|not urgent)\b",
    re.I,
)
_URGENT = re.compile(
    r"\b(asap|urgent|today|tonight|right away|by (?:tomorrow|tonight|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday)|deadline|closes?|last chance|expires?)\b",
    re.I,
)

# Automated mail is completion evidence (§7), not a to-do.
_AUTOMATED_SENDER = re.compile(
    r"^(no[-_.]?reply|donotreply|do-not-reply|orders?|receipts?|notifications?|newsletter|"
    r"news|info|support|billing|alerts?|mailer|updates?|appointments?|confirm\w*)@",
    re.I,
)
_AUTOMATED_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_PURCHASES", "CATEGORY_FORUMS", "CATEGORY_SOCIAL"}

_ACK_ONLY = re.compile(r"^\s*(ok|okay|k|kk|ha|haha|lol|thanks|thx|ty|sure|yep|yes|no|nice|cool|👍|❤️)\b[\s!.,]*$", re.I)

_DETERMINERS = {"the", "that", "this", "a", "an", "some", "my", "your", "our", "his", "her", "their"}
_EDGE_STOP = {
    "in", "on", "at", "for", "from", "with", "to", "by", "and", "but", "if", "when",
    "before", "after", "because", "so", "or", "while", "about", "of", "into", "you",
    "it", "me", "us", "them", "i", "we", "can", "that", "will", "would", "just",
    "still", "also", "then", "is", "was", "be", "doing", "up",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _URL.sub(" ", text)).strip()


def _object_phrase(text: str, trigger: re.Match) -> Optional[str]:
    """The clause immediately following the trigger, trimmed to a noun-ish core."""
    tail = text[trigger.end():].strip()
    tail = re.split(r"[.,;!?\n]|\s+—\s+", tail)[0]
    kept = [t.strip("\"'()") for t in re.split(r"\s+", tail) if t.strip("\"'()")][:6]
    # Trim filler from both ends; a phrase that is only filler is no phrase.
    while kept and kept[0].lower() in _DETERMINERS | _EDGE_STOP:
        kept.pop(0)
    while kept and kept[-1].lower() in _DETERMINERS | _EDGE_STOP:
        kept.pop()
    phrase = " ".join(kept).strip(" .,-")
    return phrase or None


def _preceding_phrase(text: str, trigger: re.Match, window: int = 3, include_trigger: bool = True) -> Optional[str]:
    """The noun phrase leading *into* the trigger — "the Marin ride Sunday",
    "the tubeless kit if you want it". Used for event nouns and for triggers
    that sit at the end of a sentence with nothing after them.

    ``include_trigger=False`` keeps only the phrase ahead of the trigger, for
    triggers that are predicates rather than part of the noun phrase.
    """
    head = text[: trigger.start()].strip()
    head = re.split(r"[.,;!?\n]|\s+—\s+", head)[-1]
    lead = [t.strip("\"'()") for t in re.split(r"\s+", head) if t.strip("\"'()")][-window:]
    kept = lead + ([trigger.group(0).strip()] if include_trigger else [])
    while kept and kept[0].lower() in _DETERMINERS | _EDGE_STOP:
        kept.pop(0)
    phrase = " ".join(kept).strip(" .,-")
    return phrase or None


def _enrich_with_proper_noun(text: str, phrase: Optional[str]) -> Optional[str]:
    """"that bag" earlier in the thread is "the Lemaire croissant bag" — reach back
    for the branded form so completion matching (§7) has something to bite on."""
    if not phrase:
        return None
    head = phrase.split()[-1].lower()
    if len(head) < 3:
        return phrase
    match = re.search(rf"\b([A-Z][\w'-]+(?:\s+[a-z][\w'-]+){{0,3}}\s+{re.escape(head)})\b", text)
    if match and len(match.group(1)) > len(phrase):
        return match.group(1)
    return phrase


def _first_url(text: str) -> Optional[str]:
    match = _URL.search(text)
    return match.group(0).rstrip(".,);") if match else None


def _date_phrase(text: str) -> Optional[str]:
    """Prefer the deadline clause over an incidental date mention."""
    deadline = re.search(
        r"\b(?:by|before|due|no later than|closes?|ends?|expires?)\s+"
        r"([A-Za-z0-9][\w\s/,'-]{0,25}?)(?=[.,;!?\n]|$| for | if | or | and )",
        text,
        re.I,
    )
    if deadline:
        return deadline.group(0).strip()
    return None


_SUBJECT_PREFIX = re.compile(
    r"^\s*(re|fwd?|updated invitation|invitation|accepted|declined|tentative|automatic reply)\s*:\s*",
    re.I,
)


def _clean_subject(subject: str) -> str:
    """Strip Re:/Fwd:/Invitation: chains and trim to a title-length phrase."""
    s = (subject or "").strip()
    for _ in range(4):
        stripped = _SUBJECT_PREFIX.sub("", s)
        if stripped == s:
            break
        s = stripped
    return s.strip()[:70]


def _action_for(item_type: str, entity: Optional[str], person: str, text: str) -> str:
    subject = entity or _clean(text)[:60]
    deadline = _date_phrase(text)
    suffix = f" ({deadline.lower()})" if deadline else ""
    return {
        "purchase": f"Buy {subject}{suffix}",
        "event": f"Prepare for {subject}{suffix}",
        "promise": f"Follow through on {subject}{suffix}",
        "followup": f"Follow up with {person} — {subject}{suffix}",
        "reading": f"Read {subject}",
        "question": f"Reply to {person} — {subject}{suffix}",
    }.get(item_type, f"Handle {subject}{suffix}")


def _reply_for(item_type: str, person: str) -> Optional[str]:
    return {
        "purchase": "on it — ordering this week",
        "event": "got it, putting it in the calendar now",
        "promise": "yep, I'll take care of it",
        "followup": "sorry, this slipped — looking at it today",
        "reading": "saving it, will read tonight",
        "question": None,
    }.get(item_type)


def _is_automated(metadata: Dict[str, Any]) -> bool:
    sender = (metadata.get("from_email") or "").strip()
    if sender and _AUTOMATED_SENDER.match(sender):
        return True
    return bool(_AUTOMATED_LABELS & set(metadata.get("labels", []) or []))


def _classify(text: str) -> Tuple[Optional[str], Optional[re.Match]]:
    """Returns the type and the match that decided it, so entity extraction
    reads off the same trigger the classification came from."""
    stripped = _clean(text)
    match = _SELF_PROMISE.search(stripped)
    if match:
        return "promise", match
    match = _REQUEST.search(stripped)
    if match:
        return "promise", match
    match = _FOLLOWUP.search(stripped)
    if match:
        return "followup", match
    match = _READING.search(stripped)
    if match and (_URL.search(text) or not _EVENT_NOUN.search(stripped)):
        return "reading", match
    match = _PURCHASE.search(stripped)
    if match:
        return "purchase", match
    has_date_word = bool(_date_phrase(text)) or bool(
        re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|"
                  r"january|february|march|april|may|june|july|august|september|october|"
                  r"november|december|in \d+ (?:day|week|month)s?|the \d{1,2}(?:st|nd|rd|th))\b",
                  stripped, re.I)
    )
    match = _EVENT_NOUN.search(stripped)
    if has_date_word and match:
        return "event", match
    match = _QUESTION.search(stripped)
    if match:
        return "question", match
    # Deliberately last: "when is the deadline?" is a question the user should
    # answer, not a date to prepare for.
    match = _DEADLINE_NOUN.search(stripped)
    if has_date_word and match:
        return "event", match
    return None, None


def classify_batch(batch: List[Dict[str, Any]], draft_replies: bool = True) -> Dict[str, Any]:
    """Same contract as ``claude.classify_batch``. `entities` is always empty:
    rules can extract an obligation's shape, but asserting facts about the
    user's world on a regex match is how the store got 37 corporate footers
    filed as people's phone numbers."""
    results: List[Dict[str, Any]] = []
    for entry in batch:
        text = entry.get("text") or ""
        metadata = entry.get("metadata") or {}
        if _ACK_ONLY.match(text) or len(_clean(text)) < 8:
            continue
        if entry.get("source") == "gmail" and _is_automated(metadata):
            continue

        item_type, trigger = _classify(text)
        if not item_type or trigger is None:
            continue

        stripped = _clean(text)
        if item_type == "event":
            # The event noun *is* the trigger, so read backwards into it. A
            # deadline trigger can be the predicate instead ("registration
            # closes Friday"), where the subject alone is the better entity —
            # "registration", not "registration closes".
            keep_trigger = not _DEADLINE_NOUN.fullmatch(trigger.group(0))
            entity_item = _preceding_phrase(stripped, trigger, include_trigger=keep_trigger) or trigger.group(0)
        else:
            entity_item = _object_phrase(stripped, trigger) or _preceding_phrase(stripped, trigger)
            entity_item = _enrich_with_proper_noun(text, entity_item)

        # For email, the subject line is a cleaner "what" than a phrase scraped
        # from the body — which often grabs a CTA ("modify your RSVP") or a
        # signature. Prefer it, stripped of Re:/Fwd:/Invitation: noise.
        if entry.get("source") == "gmail":
            subject = _clean_subject(metadata.get("subject") or "")
            if subject:
                entity_item = subject

        flexible = bool(_FLEXIBLE.search(text))
        urgent = bool(_URGENT.search(text))
        confidence = 0.55
        if urgent:
            confidence += 0.15
        if flexible:
            confidence -= 0.15
        if metadata.get("starred") or metadata.get("important"):
            confidence += 0.1

        results.append(
            {
                "message_id": entry["id"],
                "type": item_type,
                "entities": {
                    "item": entity_item,
                    "date_phrase": _date_phrase(text),
                    "link": _first_url(text),
                },
                "suggested_action": _action_for(item_type, entity_item, entry.get("person", "them"), text),
                "suggested_reply": _reply_for(item_type, entry.get("person", "them")) if draft_replies else None,
                "confidence": round(min(max(confidence, 0.1), 0.95), 2),
                "flexible_language": flexible,
                "urgent_language": urgent,
            }
        )
    return {"items": results, "entities": []}
