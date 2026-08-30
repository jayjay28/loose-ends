"""Gmail ingestion (§3).

Full inbox, not receipts-only: every message is both an extraction candidate
and potential completion evidence. Polling is incremental — ``history.list``
from the stored historyId, falling back to a bounded ``messages.list`` on the
first run or after a 404 (history expires after ~7 days).
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

import httpx

from .. import db
from ..models import Message
from . import signatures
from .base import IdentityResolver, ensure_conversation, make_message
from .google_auth import authed_client

log = logging.getLogger(__name__)

SOURCE = "gmail"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
HISTORY_KEY = "gmail:history_id"
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
    """"A real person wrote this to you" — Gmail Primary + Inbox, bulk/automated
    senders stripped — OR it's important enough that we never risk dropping it."""
    labels = set(rec.get("labelIds", []))
    if "INBOX" not in labels:
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


def scan_attachments(days: int = 90) -> Dict[str, int]:
    """Backfill attachment metadata onto messages already in the store.

    New mail gets its metadata at ingest (`normalise` → `store`), but
    `insert_messages` is INSERT OR IGNORE, so the thousands of rows stored
    before this code existed would stay blank forever without a pass that
    asks Gmail which of them carry files. Gmail answers the whole question
    with one query (`has:attachment`), so only the carriers are re-fetched —
    125 messages in a 90-day window, not 6,059.

    Metadata only; nothing is downloaded. Idempotent: a second run rewrites
    the same values.
    """
    from .google_auth import authed_client

    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
    found = updated = missing = 0
    with authed_client() as client:
        for message_id in _list_query(client, f"after:{after} has:attachment"):
            raw = _get_message(client, message_id)
            if not raw:
                continue
            attachments = extract_attachments(raw.get("payload", {}))
            if not attachments:
                continue      # has:attachment includes some inline-only mail
            found += 1
            if db.update_message_metadata(SOURCE, message_id, {"attachments": attachments}):
                updated += 1
            else:
                missing += 1  # filtered out at ingest (promotions etc.) — fine
    log.info("attachment scan: %d carriers, %d stored rows updated, %d not in store",
             found, updated, missing)
    return {"found": found, "updated": updated, "not_in_store": missing}


def import_sample(path: Path, account_email: str = "alex.carter@gmail.com") -> int:
    """Offline path — the sample corpus used for milestone testing."""
    payload = json.loads(Path(path).read_text())
    return store(payload.get("messages", []), account_email=account_email)


# --------------------------------------------------------------- live API
def _get_profile(client: httpx.Client) -> dict:
    response = client.get(f"{API}/profile")
    response.raise_for_status()
    return response.json()


def _get_message(client: httpx.Client, message_id: str) -> Optional[dict]:
    # One slow message must not abort the whole backfill: retry once on a
    # transient network/timeout error, then skip it (it'll be picked up on the
    # next incremental sync).
    for attempt in range(2):
        try:
            response = client.get(f"{API}/messages/{message_id}", params={"format": "full"})
        except httpx.HTTPError as exc:
            if attempt == 0:
                continue
            log.warning("gmail: skipping message %s after fetch error: %s", message_id, exc)
            return None
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    return None


def _list_query(client: httpx.Client, query: str) -> List[str]:
    ids: List[str] = []
    page: Optional[str] = None
    while True:
        params = {"q": query, "maxResults": 100}
        if page:
            params["pageToken"] = page
        response = client.get(f"{API}/messages", params=params)
        response.raise_for_status()
        payload = response.json()
        ids.extend(m["id"] for m in payload.get("messages", []))
        page = payload.get("nextPageToken")
        if not page:
            break
    return ids


def _list_recent_ids(client: httpx.Client, days: int) -> List[str]:
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
    # Two lists, unioned:
    #  1. Primary — exclude the noise tabs (negation, so it still works when the
    #     account has category tabs turned off; is_primary_inbound trims the rest).
    #  2. Important / Starred from ANY tab — so a warning letter or bill sitting in
    #     Updates is still listed and can't be silently dropped.
    primary = (
        f"after:{after} in:inbox "
        "-category:promotions -category:social -category:updates -category:forums"
    )
    important = f"after:{after} in:inbox (is:important OR is:starred)"
    # Your own sent mail — so the engine can see where you left off, tell when
    # you've already replied, and notice the threads you spoke into and never
    # heard back from. store() keeps is-me records; the pipeline ignores them for
    # extraction but they matter for completion, currency, and "your last word".
    sent = f"after:{after} in:sent"
    return list(dict.fromkeys(
        _list_query(client, primary) + _list_query(client, important) + _list_query(client, sent)
    ))


def _list_history_ids(client: httpx.Client, start_history_id: str) -> Optional[List[str]]:
    """Returns None when the history id is too old and a backfill is needed."""
    ids: List[str] = []
    page: Optional[str] = None
    while True:
        params = {"startHistoryId": start_history_id, "historyTypes": "messageAdded"}
        if page:
            params["pageToken"] = page
        response = client.get(f"{API}/history", params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        for record in payload.get("history", []):
            for added in record.get("messagesAdded", []):
                ids.append(added["message"]["id"])
        page = payload.get("nextPageToken")
        if not page:
            break
    return ids


def poll(resolver: Optional[IdentityResolver] = None) -> int:
    """Fetch anything new since the last run. Returns rows inserted."""
    with authed_client() as client:
        profile = _get_profile(client)
        account_email = profile.get("emailAddress", "")
        if account_email:
            # Anything reasoning about "me" later (ics PARTSTAT, for one)
            # needs to know whose mailbox this is without re-asking Google.
            db.set_sync_state("gmail:account", account_email.lower())
        cursor = db.get_sync_state(HISTORY_KEY)

        ids: Optional[List[str]] = None
        if cursor:
            ids = _list_history_ids(client, cursor)
        if ids is None:
            ids = _list_recent_ids(client, BACKFILL_DAYS)

        account = (account_email or "").lower()
        records = []
        for message_id in dict.fromkeys(ids):     # de-dupe, preserve order
            raw = _get_message(client, message_id)
            if not raw:
                continue
            rec = normalise(raw, account_email)
            _, sender = parse_from(rec.get("from", ""))
            sent_by_me = bool(account) and sender == account
            # Keep my own sent mail (the completion engine needs my replies),
            # and inbound mail only when it's a real person in Primary.
            if sent_by_me or is_primary_inbound(rec):
                records.append(rec)

        inserted = store(records, account_email=account_email, resolver=resolver)
        # Read what the new mail carried, while we still hold the client. Its
        # failure must not cost the poll — the messages are already stored.
        try:
            from . import attachments as attachments_mod
            attachments_mod.ingest_new(client, records)
        except Exception:
            log.exception("attachment ingest failed; messages kept")
        if profile.get("historyId"):
            db.set_sync_state(HISTORY_KEY, str(profile["historyId"]))
        return inserted
