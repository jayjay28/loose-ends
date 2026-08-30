"""Source adapters (§3)."""
from __future__ import annotations

from lifeline import db
from lifeline.ingestion import gcal, gmail, imessage, load_people, load_sample_corpus, whatsapp
from lifeline.ingestion.base import IdentityResolver, normalise_handle, stable_message_id


def test_handle_normalisation_collapses_phone_formats():
    assert normalise_handle("+1 (415) 555-0142") == normalise_handle("4155550142")
    assert normalise_handle("MAYA@Example.com ") == "maya@example.com"


def test_identity_resolver_matches_known_handles(sample_dir):
    load_people(sample_dir / "people.json")
    resolver = IdentityResolver()
    assert resolver.resolve("+14155550142").id == "maya"
    assert resolver.resolve("dev.shah@hey.com").id == "dev"


def test_identity_resolver_creates_provisional_person(sample_dir):
    load_people(sample_dir / "people.json")
    resolver = IdentityResolver()
    person = resolver.resolve("+19995550000", "New Person")
    assert person.display_name == "New Person"
    assert db.get_person(person.id) is not None


def test_imessage_import_is_idempotent(sample_dir):
    load_people(sample_dir / "people.json")
    first = imessage.import_export(sample_dir / "imessage_export.json")
    second = imessage.import_export(sample_dir / "imessage_export.json")
    assert first > 0
    assert second == 0, "re-importing an export must not duplicate messages"


def test_imessage_marks_user_messages(sample_dir):
    load_people(sample_dir / "people.json")
    imessage.import_export(sample_dir / "imessage_export.json")
    mine = [m for m in db.thread_messages("imessage:iMessage;-;+14155550142") if m.is_from_user]
    assert [m.text for m in mine] == ["ha, noted"]


def test_apple_epoch_conversion():
    # 2001-01-01 plus one day, in nanoseconds.
    assert imessage.apple_time_to_iso(86400 * 1_000_000_000).startswith("2001-01-02")


def test_whatsapp_bracket_format(sample_dir):
    load_people(sample_dir / "people.json")
    count = whatsapp.import_export(sample_dir / "whatsapp_dev_shah.txt", "Dev Shah")
    assert count == 6
    messages = db.thread_messages("whatsapp:dev-shah")
    assert messages[0].timestamp.startswith("2026-07-18T09:14")
    assert messages[0].person_id == "dev"
    assert any(m.is_from_user for m in messages)


def test_whatsapp_dash_format_is_day_first(sample_dir):
    load_people(sample_dir / "people.json")
    whatsapp.import_export(sample_dir / "whatsapp_priya.txt", "Priya Raman")
    messages = db.thread_messages("whatsapp:priya-raman")
    # 21/07/2026 is 21 July, not "month 21".
    assert messages[0].timestamp.startswith("2026-07-21T13:05")


def test_whatsapp_continuation_lines_are_appended(tmp_path):
    export = tmp_path / "chat.txt"
    export.write_text(
        "[7/18/26, 9:14:03 AM] Ana: first line\nsecond line\n[7/18/26, 9:15:00 AM] Ana: another\n"
    )
    whatsapp.import_export(export, "Ana")
    texts = [m.text for m in db.thread_messages("whatsapp:ana")]
    assert texts[0] == "first line\nsecond line"


def test_whatsapp_skips_system_lines(tmp_path):
    export = tmp_path / "chat.txt"
    export.write_text(
        "[7/18/26, 9:14:03 AM] Ana: Messages and calls are end-to-end encrypted.\n"
        "[7/18/26, 9:15:00 AM] Ana: <Media omitted>\n"
        "[7/18/26, 9:16:00 AM] Ana: real message here\n"
    )
    whatsapp.import_export(export, "Ana")
    assert [m.text for m in db.thread_messages("whatsapp:ana")] == ["real message here"]


def test_gmail_from_header_parsing():
    assert gmail.parse_from("Dev Shah <dev.shah@hey.com>") == ("Dev Shah", "dev.shah@hey.com")
    assert gmail.parse_from("plain@example.com") == (None, "plain@example.com")


def test_gmail_body_extraction_prefers_plain_text():
    import base64

    encode = lambda s: base64.urlsafe_b64encode(s.encode()).decode()
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": encode("the plain part")}},
            {"mimeType": "text/html", "body": {"data": encode("<p>the html part</p>")}},
        ],
    }
    assert gmail.extract_body(payload) == "the plain part"


def test_gmail_body_falls_back_to_stripped_html():
    import base64

    payload = {
        "mimeType": "text/html",
        "body": {"data": base64.urlsafe_b64encode(b"<div>hello <b>there</b></div>").decode()},
    }
    assert gmail.extract_body(payload) == "hello there"


def _mime_tree_with_attachments():
    """A real-shaped multipart/mixed: body alternative + a PDF + a nested forward."""
    import base64
    encode = lambda t: base64.urlsafe_b64encode(t.encode()).decode()
    return {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": encode("see attached")}},
                    {"mimeType": "text/html", "body": {"data": encode("<p>see attached</p>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "asthma-action-plan.pdf",
                "body": {"attachmentId": "att-123", "size": 48219},
            },
            {
                # a forwarded message carrying its own attachment, one level down
                "mimeType": "message/rfc822",
                "parts": [
                    {
                        "mimeType": "text/calendar",
                        "filename": "invite.ics",
                        "body": {"attachmentId": "att-456", "size": 1024},
                    }
                ],
            },
        ],
    }


def test_gmail_attachments_are_seen_not_dropped():
    """extract_body walks this exact tree and returns only the text; the
    filename and attachmentId on the other parts were read and discarded —
    which is why zero of the 125 attachment-bearing messages from the last 90
    days left any trace in the store."""
    payload = _mime_tree_with_attachments()
    assert gmail.extract_body(payload) == "see attached"    # body still works

    found = gmail.extract_attachments(payload)
    assert [a["filename"] for a in found] == ["asthma-action-plan.pdf", "invite.ics"]
    assert found[0] == {
        "filename": "asthma-action-plan.pdf",
        "mime": "application/pdf",
        "size": 48219,
        "attachment_id": "att-123",
    }
    # Body parts have no filename and must never appear as attachments.
    assert gmail.extract_attachments({"mimeType": "text/plain", "body": {"data": "eA=="}}) == []


def test_gmail_attachment_metadata_reaches_the_stored_message():
    raw = {
        "id": "g-att-1",
        "threadId": "t-att-1",
        "internalDate": "1756300000000",
        "labelIds": ["IMPORTANT"],
        "payload": {
            "headers": [
                {"name": "From", "value": "Nurse Alvarez <ralvarez@lakeviewschools.org>"},
                {"name": "Subject", "value": "Re: Asthma Action Plan"},
                {"name": "Date", "value": "Thu, 27 Aug 2026 09:00:00 -0400"},
            ],
            **_mime_tree_with_attachments(),
        },
    }
    rec = gmail.normalise(raw)
    assert len(rec["attachments"]) == 2
    gmail.store([rec])

    stored = db.messages_since("2000-01-01", source="gmail")[0]
    names = [a["filename"] for a in stored.metadata["attachments"]]
    assert names == ["asthma-action-plan.pdf", "invite.ics"]

    # ... and a message without any doesn't carry an empty key.
    raw_plain = {
        "id": "g-plain-1", "threadId": "t-plain-1", "internalDate": "1756300000000",
        "labelIds": ["IMPORTANT"],
        "payload": {
            "headers": [{"name": "From", "value": "a@b.com"}, {"name": "Subject", "value": "hi"}],
            "mimeType": "text/plain", "body": {"data": "aGVsbG8="},
        },
    }
    gmail.store([gmail.normalise(raw_plain)])
    plain = [m for m in db.messages_since("2000-01-01", source="gmail") if m.external_id == "g-plain-1"][0]
    assert "attachments" not in plain.metadata


def test_update_message_metadata_merges_without_clobbering():
    """The backfill path: INSERT OR IGNORE means a re-poll teaches a stored row
    nothing, so `attachments scan` writes metadata onto old rows directly."""
    raw = {
        "id": "g-old-1", "threadId": "t-old-1", "internalDate": "1756300000000",
        "labelIds": ["IMPORTANT", "STARRED"],
        "payload": {
            "headers": [{"name": "From", "value": "a@b.com"}, {"name": "Subject", "value": "old mail"}],
            "mimeType": "text/plain", "body": {"data": "aGVsbG8="},
        },
    }
    gmail.store([gmail.normalise(raw)])

    patch = {"attachments": [{"filename": "form.pdf", "mime": "application/pdf", "size": 1, "attachment_id": "a1"}]}
    assert db.update_message_metadata("gmail", "g-old-1", patch) is True
    assert db.update_message_metadata("gmail", "no-such-message", patch) is False

    row = [m for m in db.messages_since("2000-01-01", source="gmail") if m.external_id == "g-old-1"][0]
    assert row.metadata["starred"] is True          # existing keys survive the merge
    assert row.metadata["attachments"][0]["filename"] == "form.pdf"


def test_gmail_sample_records_explicit_signals(sample_dir):
    gmail.import_sample(sample_dir / "gmail_sample.json")
    starred = [m for m in db.messages_since("2000-01-01", source="gmail") if m.metadata.get("starred")]
    assert len(starred) == 1
    assert "medicare" in starred[0].text.lower()


def test_calendar_sample_captures_rsvp(sample_dir):
    gcal.import_sample(sample_dir / "calendar_sample.json")
    events = {e.id: e for e in db.list_calendar_events()}
    assert events["cal-2003"].self_response == "needsAction"
    assert events["cal-2002"].summary == "Grandma's 80th"


def test_calendar_normalise_handles_all_day_events():
    event = gcal.normalise({"id": "x", "summary": "Birthday", "start": {"date": "2026-09-11"}, "end": {"date": "2026-09-12"}})
    assert event.start_at.startswith("2026-09-11T00:00")


def test_sample_corpus_loads_every_source(sample_dir):
    counts = load_sample_corpus(sample_dir)
    assert counts["imessage"] == 12
    assert counts["whatsapp"] == 10
    assert counts["gmail"] == 8
    assert counts["calendar"] == 5


def test_stable_message_id_is_deterministic():
    assert stable_message_id("gmail", "abc") == stable_message_id("gmail", "abc")
    assert stable_message_id("gmail", "abc") != stable_message_id("imessage", "abc")


# ------------------------------------------------- Gmail Primary-only filter
def _mail(labels, frm="Sam <sam@example.com>", **kw):
    return {"labelIds": labels, "from": frm, **kw}


def test_gmail_keeps_real_people_in_primary():
    assert gmail.is_primary_inbound(_mail(["INBOX", "CATEGORY_PERSONAL"]))
    assert gmail.is_primary_inbound(_mail(["INBOX"]))  # primary often has no category label


def test_gmail_drops_promotions_social_updates():
    for cat in ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS"):
        assert not gmail.is_primary_inbound(_mail(["INBOX", cat]))


def test_gmail_drops_archived_and_bulk_and_machine_senders():
    assert not gmail.is_primary_inbound(_mail(["CATEGORY_PERSONAL"]))            # not in inbox
    assert not gmail.is_primary_inbound(_mail(["INBOX"], list_unsubscribe=True))  # newsletter
    assert not gmail.is_primary_inbound(_mail(["INBOX"], precedence="bulk"))
    assert not gmail.is_primary_inbound(_mail(["INBOX"], auto_submitted="auto-generated"))
    assert not gmail.is_primary_inbound(_mail(["INBOX"], frm="Acme <no-reply@acme.com>"))
    assert not gmail.is_primary_inbound(_mail(["INBOX"], frm="GitHub <notifications@github.com>"))


def test_gmail_never_drops_important_or_high_stakes_mail():
    # A no-reply address in the Updates tab would normally be filtered...
    noreply_update = _mail(["INBOX", "CATEGORY_UPDATES"], frm="City of X <no-reply@city.gov>",
                           subject="Monthly newsletter")
    assert not gmail.is_primary_inbound(noreply_update)
    # ...but the same shape marked Important, or with high-stakes wording, is kept.
    assert gmail.is_primary_inbound({**noreply_update, "labelIds": ["INBOX", "CATEGORY_UPDATES", "IMPORTANT"]})
    assert gmail.is_primary_inbound({**noreply_update, "subject": "FINAL NOTICE: past due balance"})
    assert gmail.is_primary_inbound({**noreply_update, "subject": "Your appointment has been rescheduled"})
    # Starred always survives.
    assert gmail.is_primary_inbound(_mail(["INBOX", "CATEGORY_PROMOTIONS", "STARRED"]))


# ------------------------------------------- reading in place, and watching
#
# Stolen from openclaw/imsg after noticing it does both. The copy this replaces
# moved 449 MB on every cycle that saw a change — a cost that grew with the
# user's history rather than with how much had been said.

def _wal_db(tmp_path):
    import sqlite3
    path = tmp_path / "chat.db"
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    return path


def test_the_live_database_is_read_without_copying(tmp_path, monkeypatch):
    from pathlib import Path
    from lifeline.ingestion import imessage

    seen = {}

    def fake_import(path, resolver=None, since=None, after_rowid=None):
        seen["path"] = Path(path)
        return 3, 99

    monkeypatch.setattr(imessage, "import_chat_db", fake_import)
    src = _wal_db(tmp_path)

    assert imessage._read(src, None, "2026-01-01T00:00:00+00:00") == (3, 99)
    assert seen["path"] == src, "it copied the database instead of reading it"


def test_it_falls_back_to_a_copy_when_the_live_read_fails(tmp_path, monkeypatch):
    """A WAL reader normally needs to write the `-shm` to take a read lock,
    which is why the copy existed. If that ever fails, the old path stands."""
    import sqlite3
    from pathlib import Path
    from lifeline.ingestion import imessage

    calls = []

    def fake_import(path, resolver=None, since=None, after_rowid=None):
        calls.append(Path(path))
        if len(calls) == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return 7, 42

    monkeypatch.setattr(imessage, "import_chat_db", fake_import)
    src = _wal_db(tmp_path)

    assert imessage._read(src, None, "2026-01-01T00:00:00+00:00") == (7, 42)
    assert calls[0] == src                      # tried in place first
    assert calls[1] != src                      # then a copy
    assert calls[1].name == "chat.db"


def test_the_watch_times_out_when_nothing_is_written(tmp_path):
    from lifeline.ingestion import imessage
    if not imessage.HAS_WATCH:
        return
    assert imessage.wait_for_change(0.4, db_path=_wal_db(tmp_path)) is False


def test_the_watch_fires_on_a_write(tmp_path):
    import sqlite3, threading, time
    from lifeline.ingestion import imessage
    if not imessage.HAS_WATCH:
        return

    path = _wal_db(tmp_path)

    def write():
        time.sleep(0.2)
        con = sqlite3.connect(path)
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
        con.close()

    threading.Thread(target=write, daemon=True).start()
    assert imessage.wait_for_change(5.0, db_path=path) is True


def test_a_missing_database_fails_no_faster_than_a_quiet_minute():
    """No Full Disk Access, or Messages never used. The first cut returned
    at once here, and `watch_imessage`'s retry loop turned that into a
    busy-wait — a full core spent asking for a file it couldn't have. The
    failure path now costs the same time a quiet timeout does, so the loop
    is paced whether or not the grant exists."""
    import time
    from lifeline.ingestion import imessage

    started = time.monotonic()
    assert imessage.wait_for_change(0.3, db_path="/nonexistent/chat.db") is False
    assert time.monotonic() - started >= 0.3


def test_the_watcher_never_writes_while_a_cycle_holds_the_lock(monkeypatch):
    """Two concurrent SQLite writers is the thing `_cycle_lock` exists to
    prevent, and the watcher is a second writer."""
    from lifeline.jobs import poller

    monkeypatch.setattr(poller, "_WATCH_LOCK_WAIT", 0.2)
    monkeypatch.setattr(poller.imessage, "poll", lambda: 5)

    assert poller._imessage_once() == 5, "it should import when nothing is running"

    poller._cycle_lock.acquire()
    try:
        assert poller._imessage_once() == 0, "it wrote while a cycle was running"
    finally:
        poller._cycle_lock.release()


# --- chat.db bodies that live only in attributedBody -------------------------

def _typedstream(text: str, long_form: bool = False) -> bytes:
    """A minimal NSKeyedArchiver blob of the shape Messages writes."""
    payload = text.encode("utf-8")
    if long_form:
        length = b"\x81" + len(payload).to_bytes(2, "little")
    else:
        length = bytes([len(payload)])
    return b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00" \
        b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + length + payload + b"\x86"


def test_attributed_body_decodes_short_and_long_strings():
    assert imessage.decode_attributed_body(_typedstream("What’s your schedule like today?")) == "What’s your schedule like today?"
    long = "x" * 300
    assert imessage.decode_attributed_body(_typedstream(long, long_form=True)) == long
    assert imessage.decode_attributed_body(None) is None
    assert imessage.decode_attributed_body(b"no marker here") is None


def test_message_text_prefers_text_and_strips_attachment_placeholder():
    assert imessage.message_text("kept", _typedstream("ignored")) == "kept"
    assert imessage.message_text(None, _typedstream("from the body")) == "from the body"
    assert imessage.message_text("￼", None) == "", "an image-only message is empty, not a placeholder"
    assert imessage.message_text(None, _typedstream("￼with caption")) == "with caption"


def test_chat_db_rows_without_text_are_still_ingested(tmp_path):
    import sqlite3
    from datetime import datetime, timezone

    src = tmp_path / "chat.db"
    c = sqlite3.connect(src)
    c.executescript(
        """
        CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat(ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT);
        CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE message(ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
                             date INTEGER, is_from_me INTEGER, handle_id INTEGER);
        CREATE TABLE chat_message_join(chat_id INTEGER, message_id INTEGER);
        INSERT INTO handle VALUES (1, '+15550001111');
        INSERT INTO chat VALUES (1, 'iMessage;-;+15550001111', NULL);
        INSERT INTO chat_handle_join VALUES (1, 1);
        """
    )
    at = int((datetime(2026, 8, 25, 12, tzinfo=timezone.utc) - imessage.APPLE_EPOCH).total_seconds() * 1_000_000_000)
    rows = [
        (1, "g1", "kept in text", None),
        (2, "g2", None, _typedstream("only in the body")),
        (3, "g3", None, _typedstream("￼")),          # image only
        (4, "g4", "￼", None),                         # old-style placeholder
    ]
    for rowid, guid, text, body in rows:
        c.execute("INSERT INTO message VALUES (?,?,?,?,?,0,1)", (rowid, guid, text, body, at + rowid))
        c.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rowid,))
    c.commit(); c.close()

    inserted, _ = imessage.import_chat_db(src)
    texts = sorted(m.text for m in db.thread_messages("imessage:iMessage;-;+15550001111"))
    assert inserted == 2
    assert texts == ["kept in text", "only in the body"]


def test_a_message_that_syncs_in_late_is_ingested_next_cycle(tmp_path, monkeypatch):
    """Audit finding #8, the repro that used to fail. The checkpoint was the
    wall clock at poll start; a message the phone received while the Mac was
    asleep reaches chat.db minutes or days later with an *older* date, lands
    behind the cursor, and was never ingested. The cursor is ROWID now —
    monotonic in sync order, the order rows actually reach this machine."""
    import sqlite3
    from datetime import datetime, timedelta, timezone

    src = tmp_path / "chat.db"
    c = sqlite3.connect(src)
    c.executescript(
        """
        CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat(ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT);
        CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE message(ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
                             date INTEGER, is_from_me INTEGER, handle_id INTEGER,
                             cache_has_attachments INTEGER DEFAULT 0);
        CREATE TABLE chat_message_join(chat_id INTEGER, message_id INTEGER);
        INSERT INTO handle VALUES (1, '+15550001111');
        INSERT INTO chat VALUES (1, 'iMessage;-;+15550001111', NULL);
        INSERT INTO chat_handle_join VALUES (1, 1);
        """
    )

    def apple(dt):
        return int((dt - imessage.APPLE_EPOCH).total_seconds() * 1_000_000_000)

    now = datetime.now(timezone.utc)
    c.execute("INSERT INTO message VALUES (1,'g-first','hello there',NULL,?,0,1,0)",
              (apple(now - timedelta(hours=2)),))
    c.execute("INSERT INTO chat_message_join VALUES (1,1)")
    c.commit()

    assert imessage.poll(db_path=src) == 1

    # iCloud delivers a message *sent before* the first poll, after it ran:
    # higher ROWID, older date. The old date-keyed cursor skipped it forever.
    c.execute("INSERT INTO message VALUES (2,'g-late','sent while the lid was shut',NULL,?,0,1,0)",
              (apple(now - timedelta(hours=26)),))
    c.execute("INSERT INTO chat_message_join VALUES (1,2)")
    c.commit()

    assert imessage.poll(db_path=src) == 1, "the late-synced message is picked up"
    texts = {m.text for m in db.thread_messages("imessage:iMessage;-;+15550001111")}
    assert "sent while the lid was shut" in texts

    # And the cursor advanced: a third run reads nothing.
    assert imessage.poll(db_path=src) == 0
