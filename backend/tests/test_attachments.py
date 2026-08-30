"""Attachment ingestion (§v2.8 phase 0.2) — the files the mail was carrying.

The 0.1 scan found 123 attachment-bearing messages in 90 days and not one ever
read; 94 of them had been filtered out of the store entirely, including birth
certificates and a child's reading diagnostic. These tests cover the three
promises 0.2 makes: parse failures become data instead of exceptions, the same
content is parsed once, and a filtered-out carrier whose document parses gets
its message ingested after all.
"""
from __future__ import annotations

import base64
import hashlib
import io

from lifeline import db
from lifeline.ingestion import attachments, gmail
from lifeline.models import Attachment

from tests.conftest import make_conversation, make_message


def _pdf_bytes(text: str = "Asthma Action Plan for Lia Carter") -> bytes:
    """A real one-page PDF, built with the same library that parses it."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    # pypdf can't draw text; a page with none extracts to "". Real text needs
    # a content stream, so write the simplest possible one by hand.
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    stream = DecodedStreamObject()
    stream.set_data(
        f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    )
    stream_ref = writer._add_object(stream)
    page[NameObject("/Contents")] = stream_ref
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
        })
    })
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ parsing

def test_pdf_text_comes_out():
    text, error = attachments.parse("application/pdf", "plan.pdf", _pdf_bytes())
    assert error is None
    assert "Asthma Action Plan for Lia Carter" in text


def test_a_broken_pdf_becomes_an_error_not_an_exception():
    text, error = attachments.parse("application/pdf", "bad.pdf", b"%PDF-not really")
    assert text is None
    assert error, "the failure must be recorded, not raised"


def test_a_scanned_pdf_reports_its_emptiness():
    """A page with no text layer extracts to nothing — the honest result, and
    the inventory OCR would work from later."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    text, error = attachments.parse("application/pdf", "scan.pdf", buf.getvalue())
    assert text is None
    assert "scanned" in error


def test_unsupported_types_are_done_not_failed():
    text, error = attachments.parse("image/jpeg", "photo.jpg", b"\xff\xd8\xff")
    assert text is None
    assert error.startswith("unsupported")


def test_text_files_decode_and_long_ones_are_capped():
    text, _ = attachments.parse("text/calendar", "invite.ics", b"BEGIN:VCALENDAR\nEND:VCALENDAR")
    assert "VCALENDAR" in text

    long = ("x" * 30_000).encode()
    text, _ = attachments.parse("text/plain", "big.txt", long)
    assert len(text) <= attachments.MAX_TEXT_CHARS + len(attachments.TRUNCATION_MARK)
    assert text.endswith(attachments.TRUNCATION_MARK)


# ------------------------------------------------------- fetch and storage

class FakeClient:
    """Just enough of httpx.Client for the attachment endpoints."""

    def __init__(self, files: dict):
        self.files = files          # attachment_id -> bytes
        self.fetches = 0

    def get(self, url, **kwargs):
        self.fetches += 1
        att_id = url.rsplit("/", 1)[-1]

        class R:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(inner):
                data = self.files.get(att_id)
                if data is None:
                    raise KeyError(att_id)
                return {"data": base64.urlsafe_b64encode(data).decode()}
        return R()


def _stored_gmail_message(external_id="g-1"):
    make_conversation("gmail:t1", source="gmail", name="school")
    return make_message(
        "see attached", conversation_id="gmail:t1", person_id=None,
        metadata={"attachments": [
            {"filename": "plan.pdf", "mime": "application/pdf", "size": 100, "attachment_id": "att-1"},
        ]},
        external_id=external_id, source="gmail",
    )


def test_ingest_stores_the_text_and_is_idempotent():
    message = _stored_gmail_message()
    client = FakeClient({"att-1": _pdf_bytes()})
    meta = message.metadata["attachments"]

    assert attachments.ingest_gmail_message(client, message, meta) == 1
    rows = db.attachments_for_message(message.id)
    assert len(rows) == 1
    assert "Asthma Action Plan" in rows[0].text
    assert rows[0].parsed_at and rows[0].error is None

    # Run it again: same file, same message, no second row.
    assert attachments.ingest_gmail_message(client, message, meta) == 0
    assert len(db.attachments_for_message(message.id)) == 1


def test_the_same_content_is_parsed_once():
    """The preschool packet arrives three times; the work happens once."""
    data = _pdf_bytes("June PTA Calendar")
    sha = hashlib.sha256(data).hexdigest()

    first = _stored_gmail_message("g-first")
    attachments.ingest_gmail_message(FakeClient({"att-1": data}), first,
                                     first.metadata["attachments"])
    assert db.attachment_text_by_sha(sha) is not None

    # Second copy on another message: fetched, but the text is reused.
    second = _stored_gmail_message("g-second")
    import lifeline.ingestion.attachments as mod
    calls = []
    original = mod.parse
    mod.parse = lambda *a: calls.append(a) or original(*a)
    try:
        attachments.ingest_gmail_message(FakeClient({"att-1": data}), second,
                                         second.metadata["attachments"])
    finally:
        mod.parse = original
    assert calls == [], "the sha match should skip the parser entirely"
    assert "June PTA Calendar" in db.attachments_for_message(second.id)[0].text


def test_an_oversize_file_is_recorded_and_never_fetched():
    message = _stored_gmail_message()
    meta = [{"filename": "huge.pdf", "mime": "application/pdf",
             "size": attachments.MAX_FETCH_BYTES + 1, "attachment_id": "att-big"}]
    client = FakeClient({})
    attachments.ingest_gmail_message(client, message, meta)

    assert client.fetches == 0
    row = db.attachments_for_message(message.id)[0]
    assert row.error.startswith("skipped")
    assert row.parsed_at, "done, not waiting for a retry"


def test_a_transient_fetch_failure_leaves_no_row():
    """No row means the next run tries again — unlike a parse failure, which
    is permanent and recorded."""
    message = _stored_gmail_message()
    attachments.ingest_gmail_message(FakeClient({}), message,
                                     message.metadata["attachments"])
    assert db.attachments_for_message(message.id) == []


# ------------------------------------------------------------- the backfill

def test_backfill_rescues_a_filtered_out_document(monkeypatch):
    """The birth-certificate case: the carrier never made it past the ingest
    filters, but its document parses, so the message is stored after all."""
    pdf = _pdf_bytes("Certificate of Birth")
    raw = {
        "id": "g-filtered", "threadId": "t-filtered", "internalDate": "1756300000000",
        "labelIds": [],
        "payload": {
            "headers": [
                {"name": "From", "value": "Alex Carter <alex.carter@gmail.com>"},
                {"name": "Subject", "value": ""},
                {"name": "Date", "value": "Wed, 1 Jul 2026 09:00:00 -0400"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"docs").decode()}},
                {"mimeType": "application/pdf", "filename": "Birth cert.pdf",
                 "body": {"attachmentId": "att-cert", "size": len(pdf)}},
            ],
        },
    }
    client = FakeClient({"att-cert": pdf})
    monkeypatch.setattr(gmail, "_list_query", lambda c, q: ["g-filtered"])
    monkeypatch.setattr(gmail, "_get_message", lambda c, mid: raw)
    import lifeline.ingestion.google_auth as ga

    class _Ctx:
        def __enter__(self):
            return client
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(ga, "authed_client", lambda: _Ctx())

    stats = attachments.backfill(days=90)
    assert stats["messages_added"] == 1
    message = db.get_message_by_external_id("gmail", "g-filtered")
    assert message is not None, "the filtered carrier was ingested"
    assert "Certificate of Birth" in db.attachments_for_message(message.id)[0].text


def test_backfill_records_a_scan_it_cannot_read(monkeypatch):
    """The actual birth-certificate case, as found live: a 7 MB scan with no
    text layer. The first version of this code skipped it silently — no
    message row, no attachment row, no trace — which is the exact invisibility
    0.1 exists to end. A document that defeats the parser is the OCR backlog,
    and the backlog must be countable."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)     # a page with no text layer
    buf = io.BytesIO()
    writer.write(buf)
    scan = buf.getvalue()

    raw = {
        "id": "g-scan", "threadId": "t-scan", "internalDate": "1756300000000",
        "labelIds": [],
        "payload": {
            "headers": [
                {"name": "From", "value": "Alex Carter <alex.carter@gmail.com>"},
                {"name": "Subject", "value": ""},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "application/pdf", "filename": "Birth cert.pdf",
                 "body": {"attachmentId": "att-scan", "size": len(scan)}},
            ],
        },
    }
    client = FakeClient({"att-scan": scan})
    monkeypatch.setattr(gmail, "_list_query", lambda c, q: ["g-scan"])
    monkeypatch.setattr(gmail, "_get_message", lambda c, mid: raw)
    import lifeline.ingestion.google_auth as ga

    class _Ctx:
        def __enter__(self):
            return client
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(ga, "authed_client", lambda: _Ctx())

    stats = attachments.backfill(days=90)
    assert stats["messages_added"] == 1
    message = db.get_message_by_external_id("gmail", "g-scan")
    row = db.attachments_for_message(message.id)[0]
    assert row.text is None
    assert "scanned" in row.error
    assert row.parsed_at, "done — the OCR backlog, not a retry loop"


def test_backfill_leaves_pure_image_mail_out(monkeypatch):
    """The promo-with-inline-logo case: nothing parses, no message row."""
    raw = {
        "id": "g-promo", "threadId": "t-promo", "internalDate": "1756300000000",
        "labelIds": ["CATEGORY_PROMOTIONS"],
        "payload": {
            "headers": [{"name": "From", "value": "deals@shop.com"},
                        {"name": "Subject", "value": "SALE"}],
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "image/png", "filename": "banner.png",
                 "body": {"attachmentId": "att-img", "size": 5000}},
            ],
        },
    }
    client = FakeClient({"att-img": b"\x89PNG fake"})
    monkeypatch.setattr(gmail, "_list_query", lambda c, q: ["g-promo"])
    monkeypatch.setattr(gmail, "_get_message", lambda c, mid: raw)
    import lifeline.ingestion.google_auth as ga

    class _Ctx:
        def __enter__(self):
            return client
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(ga, "authed_client", lambda: _Ctx())

    stats = attachments.backfill(days=90)
    assert stats["skipped"] == 1
    assert db.get_message_by_external_id("gmail", "g-promo") is None


# ------------------------------------------------------------ the ics door

_ICS = """BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
METHOD:REQUEST
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260912T150000
DTEND;TZID=America/New_York:20260912T160000
DTSTAMP:20260826T172606Z
ORGANIZER;CN=Coach:mailto:coach@lindensoccer.org
UID:9xk2plfo431bce7dgsjeqavuwq@google.com
ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=
 TRUE;CN=alex.carter@gmail.com;X-NUM-GUESTS=0:mailto:alex.carter@gmail.com
SUMMARY:Fall REC soccer - first practice
LOCATION:Linden Field
END:VEVENT
END:VCALENDAR
"""


def test_an_invite_becomes_a_calendar_event():
    from lifeline.ingestion import gcal

    db.set_sync_state("gmail:account", "alex.carter@gmail.com")
    assert gcal.import_ics(_ICS) == 1

    event = {e.id: e for e in db.list_calendar_events()}["9xk2plfo431bce7dgsjeqavuwq"]
    assert event.summary == "Fall REC soccer - first practice"
    assert event.location == "Linden Field"
    # 15:00 America/New_York (EDT, -4) is 19:00 UTC — the tz block was honored
    # even though this fixture omits the VTIMEZONE definition (icalendar knows
    # the Olson name), and the Google UID lost its @google.com suffix so the
    # Calendar API's row for the same event lands here instead of doubling.
    assert event.start_at == "2026-09-12T19:00:00+00:00"
    assert event.self_response == "needsAction"


def test_a_revised_invite_updates_in_place_and_a_cancel_cancels():
    from lifeline.ingestion import gcal

    gcal.import_ics(_ICS)
    revised = _ICS.replace("20260912T150000", "20260913T150000")
    gcal.import_ics(revised)

    events = [e for e in db.list_calendar_events() if e.id == "9xk2plfo431bce7dgsjeqavuwq"]
    assert len(events) == 1, "same UID must not duplicate"
    assert events[0].start_at == "2026-09-13T19:00:00+00:00"

    cancel = revised.replace("METHOD:REQUEST", "METHOD:CANCEL")
    gcal.import_ics(cancel)
    assert [e for e in db.list_calendar_events() if e.id == "9xk2plfo431bce7dgsjeqavuwq"][0].status == "cancelled"


def test_garbage_ics_is_a_zero_not_an_exception():
    from lifeline.ingestion import gcal

    assert gcal.import_ics("BEGIN:VCALENDAR\nnot really") == 0


def test_an_ics_attachment_reaches_the_calendar_through_ingest():
    """The wire-in: parsing an invite attachment also writes the event."""
    make_conversation("gmail:t1", source="gmail", name="soccer")
    message = make_message(
        "practice invite", conversation_id="gmail:t1", person_id=None,
        metadata={"attachments": [
            {"filename": "invite.ics", "mime": "text/calendar", "size": len(_ICS),
             "attachment_id": "att-ics"},
        ]},
        external_id="g-ics-1", source="gmail",
    )
    client = FakeClient({"att-ics": _ICS.encode()})
    attachments.ingest_gmail_message(client, message, message.metadata["attachments"])

    assert "VCALENDAR" in db.attachments_for_message(message.id)[0].text
    assert any(e.id == "9xk2plfo431bce7dgsjeqavuwq" for e in db.list_calendar_events())


def test_the_stored_ics_sweep_is_idempotent():
    """The one-time sweep over what 0.2 already captured as plain text."""
    make_conversation("gmail:t1", source="gmail", name="soccer")
    message = make_message("invite", conversation_id="gmail:t1", person_id=None,
                           external_id="g-ics-2", source="gmail")
    db.insert_attachment(Attachment(
        message_id=message.id, source="gmail", filename="invite.ics",
        mime="text/calendar", sha256="sha-ics", text=_ICS,
        parsed_at="2026-08-28T00:00:00+00:00",
    ))
    assert attachments.import_stored_ics() == 1
    assert attachments.import_stored_ics() == 1     # upsert: same row, not a second
    assert len([e for e in db.list_calendar_events() if e.id == "9xk2plfo431bce7dgsjeqavuwq"]) == 1


# ----------------------------------------------- cargo reaches the classifier

def test_attachment_text_joins_the_batch():
    """The AutoPay letters say "AutoPay Failure" in the PDF and nothing in the
    body. Classified on the body alone they are just another no-reply email."""
    from lifeline.extraction import pipeline

    make_conversation("gmail:t1", source="gmail", name="American Water")
    message = make_message(
        "An Important Message Regarding Your American Water Account",
        conversation_id="gmail:t1", person_id=None,
        metadata={"attachments": [{"filename": "AutoPay Failure.pdf"}]},
        external_id="g-water", source="gmail",
    )
    db.insert_attachment(Attachment(
        message_id=message.id, source="gmail", filename="AutoPay Failure.pdf",
        mime="application/pdf", sha256="sha-water",
        text="Your AutoPay payment of $84.20 could not be processed. "
             "Please pay by 09/15/2026 to avoid service interruption.",
        parsed_at="2026-08-28T00:00:00+00:00",
    ))

    entry = pipeline.build_batch([message])[0]
    assert "[attachment: AutoPay Failure.pdf]" in entry["text"]
    assert "$84.20" in entry["text"]
    assert "09/15/2026" in entry["text"]


def test_cargo_is_bounded_so_one_statement_cannot_eat_the_batch():
    from lifeline.extraction import pipeline

    make_conversation("gmail:t1", source="gmail", name="bank")
    message = make_message(
        "statement attached", conversation_id="gmail:t1", person_id=None,
        metadata={"attachments": [{"filename": "a.pdf"}, {"filename": "b.pdf"}]},
        external_id="g-stmt", source="gmail",
    )
    for n, name in enumerate(["a.pdf", "b.pdf"]):
        db.insert_attachment(Attachment(
            message_id=message.id, source="gmail", filename=name,
            mime="application/pdf", sha256=f"sha-{name}",
            text="x" * 20_000, parsed_at="2026-08-28T00:00:00+00:00",
        ))

    text = pipeline.build_batch([message])[0]["text"]
    assert len(text) < pipeline.ATTACHMENT_TEXT_BUDGET + 600
    assert "[more attachments omitted]" in text
    assert "[attachment: b.pdf]" not in text, "the budget was spent on a.pdf"


def test_a_message_without_cargo_is_untouched():
    from lifeline.extraction import pipeline

    make_conversation("gmail:t1", source="gmail", name="plain")
    message = make_message("just words", conversation_id="gmail:t1",
                           person_id=None, external_id="g-none", source="gmail")
    assert pipeline.build_batch([message])[0]["text"] == "just words"


# ----------------------------------------------------- iMessage attachments

def _chat_db_with_attachments(tmp_path):
    """A synthetic chat.db shaped like the real one: attachment tables, the
    join, cache_has_attachments, and files on disk where `filename` points."""
    import sqlite3 as sq
    from datetime import datetime, timezone
    from lifeline.ingestion import imessage

    src = tmp_path / "chat.db"
    c = sq.connect(src)
    c.executescript("""
        CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat(ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT);
        CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE message(ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
                             date INTEGER, is_from_me INTEGER, handle_id INTEGER,
                             cache_has_attachments INTEGER DEFAULT 0);
        CREATE TABLE chat_message_join(chat_id INTEGER, message_id INTEGER);
        CREATE TABLE attachment(ROWID INTEGER PRIMARY KEY, filename TEXT, transfer_name TEXT,
                                mime_type TEXT, total_bytes INTEGER);
        CREATE TABLE message_attachment_join(message_id INTEGER, attachment_id INTEGER);
        INSERT INTO handle VALUES (1, '+15550001111');
        INSERT INTO chat VALUES (1, 'iMessage;-;+15550001111', NULL);
        INSERT INTO chat_handle_join VALUES (1, 1);
    """)
    at = int((datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
              - imessage.APPLE_EPOCH).total_seconds() * 1_000_000_000)

    pdf_path = tmp_path / "lease.pdf"
    pdf_path.write_bytes(_pdf_bytes("Detroit lease agreement"))
    photo_path = tmp_path / "IMG_1.heic"
    photo_path.write_bytes(b"\x00heic fake")

    rows = [
        # (rowid, guid, text, has_att, [(att_rowid, path, name, mime, size)])
        (1, "m-doc", None, 1, [(11, str(pdf_path), "lease.pdf", "application/pdf",
                                pdf_path.stat().st_size)]),          # doc, no caption
        (2, "m-photo", None, 1, [(12, str(photo_path), "IMG_1.heic", "image/heic",
                                  10)]),                              # photo only
        (3, "m-both", "here's the pic", 1, [(13, str(photo_path), "IMG_1.heic",
                                             "image/heic", 10)]),     # caption + photo
        (4, "m-gone", None, 1, [(14, str(tmp_path / "missing.pdf"), "missing.pdf",
                                 "application/pdf", 10)]),            # offloaded file
    ]
    for rowid, guid, text, has_att, atts in rows:
        c.execute("INSERT INTO message VALUES (?,?,?,NULL,?,0,1,?)",
                  (rowid, guid, text, at + rowid, has_att))
        c.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rowid,))
        for att_rowid, path, name, mime, size in atts:
            c.execute("INSERT INTO attachment VALUES (?,?,?,?,?)",
                      (att_rowid, path, name, mime, size))
            c.execute("INSERT INTO message_attachment_join VALUES (?,?)", (rowid, att_rowid))
    c.commit(); c.close()
    return src


def test_a_texted_pdf_is_ingested_message_and_all(tmp_path):
    """A PDF someone texts over is a document arriving by iMessage. The old
    code dropped the whole message for having no text, and the document went
    with it."""
    from lifeline.ingestion import imessage

    imessage.import_chat_db(_chat_db_with_attachments(tmp_path))

    message = db.get_message_by_external_id("imessage", "m-doc")
    assert message is not None
    rows = db.attachments_for_message(message.id)
    assert len(rows) == 1
    assert "Detroit lease agreement" in rows[0].text
    assert rows[0].source == "imessage"


def test_a_captionless_photo_stays_out_but_a_captioned_one_is_inventory(tmp_path):
    from lifeline.ingestion import imessage

    imessage.import_chat_db(_chat_db_with_attachments(tmp_path))

    # Photo with no caption and no document: no message, as always.
    assert db.get_message_by_external_id("imessage", "m-photo") is None

    # Photo on a texted message: the message stands, the photo is recorded
    # unparsed — the inventory OCR would work from later.
    both = db.get_message_by_external_id("imessage", "m-both")
    row = db.attachments_for_message(both.id)[0]
    assert row.text is None
    assert row.error.startswith("unsupported")


def test_an_offloaded_file_leaves_no_row_for_a_later_retry(tmp_path):
    """iCloud keeps the bytes; the Mac has a stub. Transient by nature."""
    from lifeline.ingestion import imessage

    imessage.import_chat_db(_chat_db_with_attachments(tmp_path))
    message = db.get_message_by_external_id("imessage", "m-gone")
    assert message is not None, "the message is kept — it names a document"
    assert db.attachments_for_message(message.id) == []


def test_reimporting_the_chat_db_adds_nothing(tmp_path):
    from lifeline.ingestion import imessage

    src = _chat_db_with_attachments(tmp_path)
    imessage.import_chat_db(src)
    first = db.get_connection().execute("select count(*) from attachments").fetchone()[0]
    imessage.import_chat_db(src)
    assert db.get_connection().execute("select count(*) from attachments").fetchone()[0] == first


# ------------------------------------------------- the Apple Calendar door

def _apple_store(tmp_path, rows):
    """A synthetic Calendar.sqlitedb in the shape the reader expects."""
    import sqlite3 as sq
    src = tmp_path / "Calendar.sqlitedb"
    c = sq.connect(src)
    c.executescript("""
        CREATE TABLE Calendar(ROWID INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE Location(ROWID INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE CalendarItem(ROWID INTEGER PRIMARY KEY, summary TEXT,
            start_date REAL, end_date REAL, all_day INTEGER DEFAULT 0,
            status INTEGER DEFAULT 0, external_id TEXT, UUID TEXT,
            description TEXT, location_id INTEGER, calendar_id INTEGER,
            hidden INTEGER DEFAULT 0);
        INSERT INTO Calendar VALUES (1, 'alex.carter@gmail.com');
        INSERT INTO Location VALUES (1, 'Linden Field');
    """)
    for r in rows:
        c.execute("INSERT INTO CalendarItem VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", r)
    c.commit(); c.close()
    return src


def _apple_secs(days_ahead):
    from datetime import datetime, timedelta, timezone
    from lifeline.ingestion.applecal import APPLE_EPOCH
    return (datetime.now(timezone.utc) + timedelta(days=days_ahead) - APPLE_EPOCH).total_seconds()


def test_apple_calendar_converges_on_the_ics_door(tmp_path):
    """The same Google event through the API, an invite, and the local store
    must be one row — the UID's @google.com suffix is the shared key."""
    from lifeline.ingestion import applecal, gcal

    gcal.import_ics(_ICS)          # writes id 9xk2plfo431bce7dgsjeqavuwq
    src = _apple_store(tmp_path, [
        (1, "Fall REC soccer - first practice", _apple_secs(3), _apple_secs(3) + 3600,
         0, 0, "9xk2plfo431bce7dgsjeqavuwq@google.com", "UUID-1", "", 1, 1, 0),
    ])
    assert applecal.poll(db_path=src) == 1
    rows = [e for e in db.list_calendar_events() if e.id == "9xk2plfo431bce7dgsjeqavuwq"]
    assert len(rows) == 1, "three doors, one row"
    assert rows[0].location == "Linden Field", "the local store enriched it"


def test_apple_calendar_skips_hidden_cancelled_and_out_of_window(tmp_path):
    from lifeline.ingestion import applecal

    src = _apple_store(tmp_path, [
        (1, "Dentist", _apple_secs(5), _apple_secs(5) + 1800, 0, 0, "", "U-a", "", None, 1, 0),
        (2, "Cancelled thing", _apple_secs(6), None, 0, 3, "", "U-b", "", None, 1, 0),
        (3, "Hidden thing", _apple_secs(7), None, 0, 0, "", "U-c", "", None, 1, 1),
        (4, "Ancient thing", _apple_secs(-400), None, 0, 0, "", "U-d", "", None, 1, 0),
    ])
    applecal.poll(db_path=src)
    by_summary = {e.summary: e for e in db.list_calendar_events()}
    assert "Dentist" in by_summary
    assert by_summary["Cancelled thing"].status == "cancelled"
    assert "Hidden thing" not in by_summary
    assert "Ancient thing" not in by_summary


def test_apple_calendar_sameness_guard_without_an_external_id(tmp_path):
    """A local row with no recognisable external id must not duplicate an
    event another door already wrote."""
    from lifeline.ingestion import applecal, gcal
    from lifeline.models import CalendarEvent

    when = _apple_secs(2)
    from lifeline.ingestion.applecal import _when
    db.upsert_calendar_events([CalendarEvent(
        id="from-the-api", calendar_id="primary",
        summary="Team standup", start_at=_when(when))])

    src = _apple_store(tmp_path, [
        (1, "Team standup", when, None, 0, 0, "", "U-x", "", None, 1, 0),
    ])
    assert applecal.poll(db_path=src) == 0
    assert len([e for e in db.list_calendar_events() if e.summary == "Team standup"]) == 1


def test_a_strange_schema_is_a_logged_zero_not_a_crash(tmp_path):
    import sqlite3 as sq
    from lifeline.ingestion import applecal

    src = tmp_path / "Calendar.sqlitedb"
    c = sq.connect(src)
    c.execute("CREATE TABLE SomethingElse(x)")
    c.commit(); c.close()
    assert applecal.poll(db_path=src) == 0
