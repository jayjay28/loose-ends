"""Attachment ingestion (§v2.8 phase 0.2) — the files the mail was carrying.

Phase 0.1 made the gap countable: 123 attachment-bearing messages in 90 days
and not one ever read. This module reads them. Fetch by Gmail attachmentId,
hash, parse by type, store the text next to the message — so a school form or
an AutoPay-failure letter is finally *in* the store rather than named by it.

Rules, in order of importance:

* **Never raise.** A parse failure writes `error` and sets `parsed_at`; the
  row is done, not retried forever. Same law the 08-27 audit asked for on
  classifier output — one bad PDF must not kill a cycle.
* **Hash before parsing.** The same packet arrives three times; the work
  happens once (`db.attachment_text_by_sha`).
* **Fetch from the carrier list, not from the store.** The 0.1 scan found 94
  of 123 carriers were filtered out at ingest — birth certificates, a child's
  reading diagnostic, AutoPay-failure letters. A carrier holding a *document*
  gets its message ingested too, parsed or not: a 7 MB scan that defeats the
  parser is the OCR backlog and must leave a row saying so, and `donotreply@`
  is how institutions send exactly the documents that matter.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from .. import db
from ..models import Attachment, now_iso

log = logging.getLogger(__name__)

# A 400-page statement is not worth its tokens. Capped with a marker so the
# truncation is visible to whatever reads the text later.
MAX_TEXT_CHARS = 20_000
TRUNCATION_MARK = "\n[… truncated]"

# Over this we don't even fetch. Recorded with an error naming the size, so
# the row is done and the reason is in the data.
MAX_FETCH_BYTES = 10 * 1024 * 1024

# What we can turn into text today. Everything else is recorded, not parsed —
# `error` says why, phase 5 (OCR, office formats) can query for its backlog.
_TEXT_MIMES = ("text/plain", "text/csv", "text/calendar", "text/html", "application/csv")


def parse(mime: str, filename: str, data: bytes) -> Tuple[Optional[str], Optional[str]]:
    """(text, error) out of one file's bytes. Exactly one side is non-None,
    except an unsupported type, which is (None, 'unsupported: …') — done, not
    failed."""
    mime = (mime or "").lower().split(";")[0].strip()
    name = (filename or "").lower()

    try:
        if mime == "application/pdf" or name.endswith(".pdf"):
            return _parse_pdf(data)
        if mime in _TEXT_MIMES or name.endswith((".txt", ".csv", ".ics", ".vcf")):
            text = data.decode("utf-8", errors="replace").strip()
            return (_cap(text), None) if text else (None, "empty text file")
        return None, f"unsupported: {mime or name or 'unknown type'}"
    except Exception as exc:  # the never-raise rule: the error becomes data
        return None, f"{type(exc).__name__}: {exc}"


def _parse_pdf(data: bytes) -> Tuple[Optional[str], Optional[str]]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")          # a blank owner password is common
        except Exception:
            return None, "encrypted pdf"
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
        if sum(len(p) for p in pages) > MAX_TEXT_CHARS:
            break
    text = "\n".join(pages).strip()
    if not text:
        # A scanned form extracts to nothing. Recording that emptiness is the
        # honest result — and it is the inventory OCR would work from later.
        return None, "no extractable text (scanned?)"
    return _cap(text), None


def _cap(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + TRUNCATION_MARK


def _is_ics(mime: str, filename: str) -> bool:
    return (mime or "").lower().startswith("text/calendar") or \
        (filename or "").lower().endswith(".ics")


def import_stored_ics() -> int:
    """One-time sweep: hand every already-stored .ics text to the calendar.
    The 0.2 backfill captured ~44 of them as plain text before this existed."""
    rows = db.get_connection().execute(
        "SELECT filename, mime, text FROM attachments "
        "WHERE text IS NOT NULL AND (mime LIKE 'text/calendar%' OR filename LIKE '%.ics')"
    ).fetchall()
    from . import gcal

    written = 0
    for row in rows:
        written += gcal.import_ics(row["text"])
    log.info("ics sweep: %d attachments -> %d calendar events", len(rows), written)
    return written


# ------------------------------------------------------------------- gmail

def _fetch_gmail(client, message_external_id: str, attachment_id: str) -> Optional[bytes]:
    """One attachment's bytes, or None on any failure (logged, never raised)."""
    try:
        response = client.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/"
            f"{message_external_id}/attachments/{attachment_id}"
        )
        response.raise_for_status()
        data = response.json().get("data", "")
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding)
    except Exception as exc:
        log.warning("attachment fetch failed for %s/%s: %s",
                    message_external_id, attachment_id, exc)
        return None


def ingest_gmail_message(client, message, metadata_attachments) -> int:
    """Fetch, parse and store every attachment one stored message names.
    Returns rows inserted. Idempotent — (message_id, sha256) dedupes."""
    inserted = 0
    for meta in metadata_attachments or []:
        filename = meta.get("filename") or ""
        mime = meta.get("mime") or ""
        size = int(meta.get("size") or 0)
        remote_id = meta.get("attachment_id")

        if size > MAX_FETCH_BYTES:
            record = Attachment(
                message_id=message.id, source="gmail", remote_id=remote_id,
                filename=filename, mime=mime, size_bytes=size,
                sha256=f"unfetched:{remote_id or filename}",
                parsed_at=now_iso(), error=f"skipped: {size} bytes",
            )
            inserted += db.insert_attachment(record)
            continue

        data = _fetch_gmail(client, message.external_id, remote_id) if remote_id else None
        if data is None:
            continue        # transient — leave no row, the next run retries

        sha = hashlib.sha256(data).hexdigest()
        known = db.attachment_text_by_sha(sha)
        if known is not None:
            text, error = known, None
        else:
            text, error = parse(mime, filename, data)

        # An invite is structured data wearing a text file: hand it to the
        # calendar too. Upsert-on-UID makes a re-run harmless, and its failure
        # must not cost the attachment row below.
        if text and _is_ics(mime, filename):
            try:
                from . import gcal
                gcal.import_ics(text)
            except Exception:
                log.exception("ics import failed for %s", filename)

        inserted += db.insert_attachment(Attachment(
            message_id=message.id, source="gmail", remote_id=remote_id,
            filename=filename, mime=mime, size_bytes=size or len(data),
            sha256=sha, text=text, parsed_at=now_iso(), error=error,
        ))
    return inserted


def ingest_new(client, records) -> int:
    """The poll path: after `store()`, read what the new mail carried."""
    count = 0
    for rec in records:
        if not rec.get("attachments"):
            continue
        message = db.get_message_by_external_id("gmail", rec["id"])
        if message is None:
            continue
        try:
            count += ingest_gmail_message(client, message, rec["attachments"])
        except Exception:
            log.exception("attachment ingest failed for %s", rec["id"])
    return count


def backfill(days: int = 90, limit: Optional[int] = None) -> Dict[str, int]:
    """Read everything the last `days` of mail carried.

    Works from Gmail's own carrier list (`has:attachment`), not from the
    store — the store only ever saw 29 of 123 carriers. A carrier that was
    filtered out at ingest gets its message stored if it holds anything
    document-shaped; only mail whose sole cargo is images stays out.

    A deliberate one-time job with a ceiling (`limit`), never something the
    poller drifts into.
    """
    from . import gmail
    from .google_auth import authed_client

    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
    stats = {"carriers": 0, "attachments": 0, "messages_added": 0, "skipped": 0}

    with authed_client() as client:
        ids = gmail._list_query(client, f"after:{after} has:attachment")
        if limit:
            ids = ids[:limit]
        for message_id in ids:
            try:
                raw = gmail._get_message(client, message_id)
                if not raw:
                    continue
                rec = gmail.normalise(raw)
                if not rec.get("attachments"):
                    continue
                stats["carriers"] += 1

                message = db.get_message_by_external_id("gmail", message_id)
                if message is None:
                    # Filtered at ingest. A carrier earns a message row when it
                    # holds a *document* — parsed or not. The birth certificates
                    # are 7 MB scans with no text layer; skipping them silently
                    # left no trace anywhere, which is the exact invisibility
                    # 0.1 exists to end. What stays out is mail whose only
                    # cargo is images: the promo-with-inline-logo case.
                    if not _carries_document(rec):
                        stats["skipped"] += 1
                        continue
                    gmail.store([rec])
                    message = db.get_message_by_external_id("gmail", message_id)
                    if message is None:
                        continue
                    stats["messages_added"] += 1

                stats["attachments"] += ingest_gmail_message(
                    client, message, rec["attachments"]
                )
            except Exception:
                log.exception("attachment backfill failed for %s", message_id)

    log.info("attachment backfill: %s", stats)
    return stats


# Cargo that reads as a document someone kept on purpose, whether or not any
# text comes out of it today. A scan that defeats the parser is the OCR
# backlog, not noise — it must leave a row saying so.
_DOCUMENT_MIMES = ("application/pdf", "text/calendar", "text/csv", "text/plain",
                   "application/msword", "application/vnd.openxmlformats")
_DOCUMENT_SUFFIXES = (".pdf", ".ics", ".csv", ".txt", ".vcf",
                      ".doc", ".docx", ".xls", ".xlsx")


def _carries_document(rec) -> bool:
    """Metadata-only: does any attachment look like a document? No fetch —
    the type and filename already answer it."""
    for meta in rec.get("attachments", []):
        mime = (meta.get("mime") or "").lower()
        name = (meta.get("filename") or "").lower()
        if mime.startswith(_DOCUMENT_MIMES) or name.endswith(_DOCUMENT_SUFFIXES):
            return True
    return False
