"""Mail, mapped (§3).

Full inbox, not receipts-only: every message is both an extraction candidate
and potential completion evidence.

This module owns the *shape* of mail — parsing an address, flattening HTML,
deciding whether a message is a person writing to you or a robot, and turning
a record into stored `Message` rows with their identities resolved. It reads
no mail itself; `applemail.py` is the door, and the sample corpus comes in
through `import_sample`.

§v3 removed the Gmail API door entirely rather than keeping two. It cost a
Google Cloud project, an OAuth client and a consent screen per user — the
worst stretch of onboarding by a distance — to reach mail that macOS already
keeps on disk, behind the Full Disk Access grant `chat.db` needs anyway.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .. import db
from ..models import Message
from . import signatures
from .base import IdentityResolver, ensure_conversation, make_message

log = logging.getLogger(__name__)

# The stored source name for every mail row, whatever client wrote it.
SOURCE = "mail"
BACKFILL_DAYS = 30

_ADDR_WITH_NAME = re.compile(r"^\s*\"?(?P<name>[^\"<]*?)\"?\s*<(?P<email>[^<>\s]+@[^<>\s]+)>\s*$")
_ADDR_BARE = re.compile(r"^\s*<?(?P<email>[^<>\s]+@[^<>\s]+)>?\s*$")
_HTML_TAG = re.compile(r"<[^>]+>")
# Tag-stripping alone leaves the *contents* of style/script blocks behind —
# which is why the median stored marketing mail was kilobytes of mso
# conditional comments and inlined CSS wearing a text column. Remove the
# payload blocks before the tags.
_HTML_NOISE = re.compile(
    r"<(style|script|head)\b[^>]*>.*?</\1>|<!--.*?-->",
    re.I | re.S,
)


def parse_from(value: str) -> Tuple[Optional[str], str]:
    """"Dev Shah <dev@hey.com>" -> ("Dev Shah", "dev@hey.com")."""
    m = _ADDR_WITH_NAME.match(value or "")
    if m:
        return (m.group("name").strip() or None), m.group("email").strip().lower()
    m = _ADDR_BARE.match(value or "")
    if m:
        return None, m.group("email").strip().lower()
    return None, (value or "").strip()


def _decode(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


def extract_body(payload: dict) -> str:
    """Depth-first walk preferring text/plain, falling back to stripped HTML."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime == "text/plain" and body.get("data"):
        return _decode(body["data"])
    html_fallback = ""
    for part in payload.get("parts", []) or []:
        found = extract_body(part)
        if found:
            if part.get("mimeType") == "text/html" and not html_fallback:
                html_fallback = found
            else:
                return found
    if not payload.get("parts") and mime == "text/html" and body.get("data"):
        html_fallback = _decode(body["data"])
    if html_fallback:
        text = _HTML_TAG.sub(" ", _HTML_NOISE.sub(" ", html_fallback))
        return re.sub(r"\s+", " ", text).strip()
    return ""


def extract_attachments(payload: dict) -> List[Dict[str, object]]:
    """What the message carries besides its body.

    `extract_body` walks this same tree and recurses straight through every
    attachment part — the `filename` and `attachmentId` sitting on them were
    read and dropped, which is why zero of the 125 attachment-bearing messages
    from the last 90 days left any trace in the store. This walk keeps them.

    Metadata only: nothing is downloaded here. Knowing what exists is a
    separate, cheap step from fetching it, and it is the step that makes the
    gap visible (`lifeline doctor` counts what has never been read).
    """
    found: List[Dict[str, object]] = []

    def walk(part: dict) -> None:
        filename = (part.get("filename") or "").strip()
        body = part.get("body", {}) or {}
        if filename and (body.get("attachmentId") or body.get("data")):
            found.append(
                {
                    "filename": filename,
                    "mime": part.get("mimeType", ""),
                    "size": int(body.get("size") or 0),
                    # Inline data (rare, small) has no id; the fetch step
                    # reads it straight from the part instead.
                    "attachment_id": body.get("attachmentId"),
                }
            )
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload or {})
    return found


def _headers(message: dict) -> Dict[str, str]:
    return {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}


def _iso(value: str, fallback_ms: Optional[str] = None) -> str:
    if value:
        try:
            return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            pass
    if fallback_ms:
        return datetime.fromtimestamp(int(fallback_ms) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise(raw: dict, account_email: str = "") -> dict:
    """Gmail API message -> the flat shape the rest of the pipeline uses."""
    h = _headers(raw)
    return {
        "id": raw["id"],
        "threadId": raw.get("threadId", raw["id"]),
        "from": h.get("from", ""),
        "to": h.get("to", ""),
        "subject": h.get("subject", ""),
        "date": _iso(h.get("date", ""), raw.get("internalDate")),
        "labelIds": raw.get("labelIds", []),
        "snippet": raw.get("snippet", ""),
        "body": extract_body(raw.get("payload", {})) or raw.get("snippet", ""),
        "attachments": extract_attachments(raw.get("payload", {})),
        # Markers used to tell a real person's mail from bulk/automated mail.
        "list_unsubscribe": bool(h.get("list-unsubscribe")),
        "precedence": h.get("precedence", "").lower(),
        "auto_submitted": h.get("auto-submitted", "").lower(),
    }


# Gmail's own tabs. Anything tagged one of these is not Primary.
_NON_PRIMARY = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS"}
_MACHINE_SENDER = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notification|mailer|bounce|postmaster)",
    re.I,
)
# High-stakes mail that must never be filtered out, even from a no-reply address
# or the Updates tab. Missing a warning letter, a bill, or an appointment change
# is far worse than letting a little noise through.
_HIGH_STAKES = re.compile(
    r"\b(warning|notice|past[\s-]?due|overdue|final notice|invoice|bill|payment|"
    r"amount due|balance due|appointment|reschedul|court|legal|summons|violation|"
    r"suspend|deactivat|action required|verify your|deadline|deposit|deed|escrow)\b",
    re.I,
)


def is_important_mail(rec: dict) -> bool:
    """Gmail flagged it Important or you Starred it, or the subject reads as
    high-stakes. This overrides the noise filter — importance is about who/what,
    not whether the sender happens to be automated."""
    labels = set(rec.get("labelIds", []))
    if "IMPORTANT" in labels or "STARRED" in labels:
        return True
    return bool(_HIGH_STAKES.search(rec.get("subject", "") or ""))


def is_primary_inbound(rec: dict) -> bool:
    """"A real person wrote this to you" — in the inbox, bulk and automated
    senders stripped — OR important enough that we never risk dropping it.

    "In the inbox" is stated by the record rather than inferred from a label
    vocabulary: a file in Mail's store knows it by which mailbox it sits in,
    and the sample corpus knows it by its Gmail labels. The rest of the rules
    read plain RFC headers, which both doors have.
    """
    labels = set(rec.get("labelIds", []))
    if not rec.get("in_inbox", "INBOX" in labels):
        return False
    # Safety override: keep anything important regardless of tab or sender.
    if is_important_mail(rec):
        return True
    if labels & _NON_PRIMARY:
        return False
    if rec.get("list_unsubscribe"):
        return False
    if rec.get("precedence") in ("bulk", "list", "junk"):
        return False
    if rec.get("auto_submitted", "no") not in ("", "no"):
        return False
    _, email = parse_from(rec.get("from", ""))
    if email and _MACHINE_SENDER.search(email):
        return False
    return True


def store(records: Iterable[dict], account_email: str = "", resolver: Optional[IdentityResolver] = None) -> int:
    """Persist normalised Gmail records as Messages."""
    resolver = resolver or IdentityResolver()
    account = (account_email or "").lower()
    messages: List[Message] = []
    for rec in records:
        name, email = parse_from(rec.get("from", ""))
        is_me = bool(account) and email == account
        person = None if is_me else resolver.resolve(email, name)
        if person is not None:
            # §v1.4 identity harvesting: their signature's phone number links
            # their iMessage thread to this same person (cross-channel-state.md).
            try:
                signatures.harvest(person, rec.get("body") or "")
            except Exception:  # never let identity work break ingestion
                log.exception("signature harvest failed for %s", person.id)
        conversation_id = f"{SOURCE}:{rec.get('threadId') or rec['id']}"
        # Name the thread after the counterpart, not the subject: §8.2's
        # Threads view is per-person.
        ensure_conversation(conversation_id, SOURCE, name or email or rec.get("subject") or "(no subject)", is_group=False)
        labels = rec.get("labelIds", [])
        subject = rec.get("subject", "")
        body = (rec.get("body") or rec.get("snippet") or "").strip()
        text = f"{subject}\n\n{body}".strip() if subject else body
        messages.append(
            make_message(
                source=SOURCE,
                conversation_id=conversation_id,
                external_id=rec["id"],
                timestamp=rec["date"],
                text=text,
                person_id=person.id if person else None,
                is_from_user=is_me,
                metadata={
                    "subject": subject,
                    "from_email": email,
                    "labels": labels,
                    # §6.2 explicit signals
                    "starred": "STARRED" in labels,
                    "important": "IMPORTANT" in labels,
                    "promotional": "CATEGORY_PROMOTIONS" in labels,
                    "purchase": "CATEGORY_PURCHASES" in labels,
                    # Present only when the message actually carries any, so
                    # the common case stays lean and `LIKE '%"attachments"%'`
                    # counts carriers exactly.
                    **({"attachments": rec["attachments"]} if rec.get("attachments") else {}),
                },
            )
        )
    return db.insert_messages(messages)


def import_sample(path: Path, account_email: str = "alex.carter@gmail.com") -> int:
    """Offline path — the sample corpus used for milestone testing."""
    payload = json.loads(Path(path).read_text())
    return store(payload.get("messages", []), account_email=account_email)
