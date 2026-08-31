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
    from . import invites

    written = 0
    for row in rows:
        written += invites.import_ics(row["text"])
    log.info("ics sweep: %d attachments -> %d calendar events", len(rows), written)
    return written


# -------------------------------------------------------------------- mail

def ingest_email(message, email_message) -> int:
    """Parse and store every file a mail message carries. Returns rows
    inserted; idempotent, since ``(message_id, sha256)`` dedupes.

    §v3: the bytes arrive already in hand. The Gmail door had to fetch each
    attachment over the API — a client, a remote id, and a transient failure
    mode per file — but a ``.emlx`` on disk *is* the whole message, parts
    included, so there is nothing to fetch and nothing to retry.
    """
    inserted = 0
    for part in email_message.walk() if email_message.is_multipart() else []:
        filename = part.get_filename()
        if not filename:
            continue
        mime = part.get_content_type()
        try:
            data = part.get_payload(decode=True)
        except Exception:
            data = None
        if data is None:
            continue
        if len(data) > MAX_FETCH_BYTES:
            inserted += db.insert_attachment(Attachment(
                message_id=message.id, source="mail", filename=filename,
                mime=mime, size_bytes=len(data),
                sha256=f"unfetched:{filename}", parsed_at=now_iso(),
                error=f"skipped: {len(data)} bytes",
            ))
            continue

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
                from . import invites
                invites.import_ics(text)
            except Exception:
                log.exception("ics import failed for %s", filename)

        inserted += db.insert_attachment(Attachment(
            message_id=message.id, source="mail", filename=filename,
            mime=mime, size_bytes=len(data), sha256=sha, text=text,
            parsed_at=now_iso(), error=error,
        ))
    return inserted


def _carries_document(rec) -> bool:
    """Metadata-only: does any attachment look like a document? No fetch —
    the type and filename already answer it."""
    for meta in rec.get("attachments", []):
        mime = (meta.get("mime") or "").lower()
        name = (meta.get("filename") or "").lower()
        if mime.startswith(_DOCUMENT_MIMES) or name.endswith(_DOCUMENT_SUFFIXES):
            return True
    return False
