"""Apple Mail ingestion (§v3) — the door that replaced the Gmail API.

These build a real `V10` tree of real `.emlx` files, because that is the only
part of this that is genuinely Apple's: the byte-count prefix, the mailbox
layout, and the fact that the whole thing is unreadable without Full Disk
Access. Everything downstream is the mapping the pipeline already had.
"""
from __future__ import annotations

import os
import time
from email.message import EmailMessage
from pathlib import Path

import pytest

from lifeline import db
from lifeline.ingestion import applemail

from tests.conftest import make_person


def write_emlx(mailbox: Path, name: str, message: EmailMessage,
               mtime: float | None = None) -> Path:
    """One message on disk in Mail's own shape: a byte count, a newline, the
    RFC-822 bytes, then Apple's plist trailer."""
    mailbox.mkdir(parents=True, exist_ok=True)
    raw = message.as_bytes()
    path = mailbox / name
    path.write_bytes(
        str(len(raw)).encode() + b"\n" + raw +
        b"<?xml version=\"1.0\"?><plist version=\"1.0\"><dict/></plist>\n"
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def mail_from(sender: str, subject: str, body: str = "hello",
              to: str = "alex.carter@gmail.com", **headers) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = "Mon, 31 Aug 2026 09:00:00 -0400"
    message["Message-ID"] = f"<{subject.replace(' ', '-')}@example.com>"
    for key, value in headers.items():
        message[key.replace("_", "-")] = value
    message.set_content(body)
    return message


@pytest.fixture()
def store(tmp_path) -> Path:
    """A Mail store with the two mailboxes that matter: an inbox and a sent
    folder, under the versioned directory Mail actually uses."""
    root = tmp_path / "Mail"
    (root / "V10" / "MailData").mkdir(parents=True)
    (root / "V9").mkdir()          # an older version, left behind
    return root


# ------------------------------------------------------------- finding it
def test_the_newest_version_directory_wins(store, monkeypatch):
    monkeypatch.setattr(applemail, "MAIL_ROOT", store)
    assert applemail.store_root().name == "V10"


def test_no_mail_at_all_is_none_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(applemail, "MAIL_ROOT", tmp_path / "nope")
    assert applemail.store_root() is None
    assert applemail.available() is False


def test_an_unreadable_store_is_reported_as_permission_not_absence(store, monkeypatch):
    """Without Full Disk Access the directory exists and listing it raises.
    Every caller has to survive that, and the wizard has to be able to say
    which of the two problems it is."""
    monkeypatch.setattr(applemail, "MAIL_ROOT", store)

    def denied(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "iterdir", denied)
    assert applemail.store_root() is None       # never raises
    assert applemail.permission_denied() is True


# ---------------------------------------------------------------- parsing
def test_an_emlx_is_read_past_its_byte_count(store, monkeypatch):
    monkeypatch.setattr(applemail, "MAIL_ROOT", store)
    inbox = store / "V10" / "acct" / "INBOX.mbox" / "Messages"
    path = write_emlx(inbox, "1.emlx", mail_from("dev@example.com", "padel thursday"))

    parsed = applemail.read_emlx(path)
    assert parsed is not None
    assert parsed["Subject"] == "padel thursday"


def test_a_truncated_file_is_skipped_not_raised(tmp_path):
    path = tmp_path / "bad.emlx"
    path.write_bytes(b"not-a-number\nnot a message either")
    # No length prefix: the parser takes what it can rather than exploding.
    assert applemail.read_emlx(path) is not None
    missing = tmp_path / "gone.emlx"
    assert applemail.read_emlx(missing) is None


def test_html_only_mail_is_flattened_to_readable_text(store):
    message = EmailMessage()
    message["From"] = "shop@example.com"
    message["Subject"] = "your order"
    message["Message-ID"] = "<html-1@example.com>"
    message["Date"] = "Mon, 31 Aug 2026 09:00:00 -0400"
    message.set_content("<html><style>p{color:red}</style><p>Order shipped</p></html>",
                        subtype="html")

    record = applemail.normalise(message, Path("/x/INBOX.mbox/Messages/1.emlx"))
    assert "Order shipped" in record["body"]
    assert "color:red" not in record["body"], "style payload is noise, not text"


def test_the_message_id_is_the_external_id(store):
    message = mail_from("dev@example.com", "coffee")
    record = applemail.normalise(message, Path("/x/INBOX.mbox/Messages/9.emlx"))
    assert record["id"] == "applemail:coffee@example.com"


def test_a_reply_threads_onto_its_root(store):
    message = mail_from("dev@example.com", "Re: the trip")
    message["References"] = "<root-1@example.com> <mid-2@example.com>"
    record = applemail.normalise(message, Path("/x/INBOX.mbox/Messages/2.emlx"))
    assert record["threadId"] == "root-1@example.com", "the conversation's first message"


# ----------------------------------------------------------------- polling
def _poll(store, monkeypatch, **kw):
    monkeypatch.setattr(applemail, "MAIL_ROOT", store)
    return applemail.poll(root=store / "V10", **kw)


def test_a_person_writing_to_you_is_stored(store, monkeypatch):
    make_person("dev", "Dev Shah", handles=["dev@example.com"])
    inbox = store / "V10" / "acct" / "INBOX.mbox" / "Messages"
    write_emlx(inbox, "1.emlx", mail_from("Dev Shah <dev@example.com>",
                                          "padel thursday", "7pm, usual courts?"))

    assert _poll(store, monkeypatch) == 1
    stored = db.messages_since("2000-01-01", source="mail")
    assert len(stored) == 1
    assert "usual courts" in stored[0].text
    assert stored[0].person_id == "dev"


def test_a_robot_is_not_a_person(store, monkeypatch):
    """The same bulk-mail rule the pipeline always had, now reading real
    headers instead of Google's label vocabulary."""
    inbox = store / "V10" / "acct" / "INBOX.mbox" / "Messages"
    write_emlx(inbox, "1.emlx", mail_from(
        "Shop <no-reply@shop.example>", "50% off everything",
        list_unsubscribe="<mailto:stop@shop.example>"))

    assert _poll(store, monkeypatch) == 0
    assert db.messages_since("2000-01-01", source="mail") == []


def test_my_own_sent_mail_is_kept_as_evidence(store, monkeypatch):
    """The completion engine closes loops on the strength of my replies, so
    the Sent mailbox is not optional."""
    sent = store / "V10" / "acct" / "Sent Messages.mbox" / "Messages"
    write_emlx(sent, "1.emlx", mail_from("alex.carter@gmail.com", "re: the quote",
                                         to="builder@example.com"))

    assert _poll(store, monkeypatch) == 1
    stored = db.messages_since("2000-01-01", source="mail")[0]
    assert stored.is_from_user is True


def test_trash_and_junk_are_never_read(store, monkeypatch):
    for box in ("Trash", "Junk", "Deleted Messages"):
        folder = store / "V10" / "acct" / f"{box}.mbox" / "Messages"
        write_emlx(folder, "1.emlx", mail_from("dev@example.com", f"from {box}"))

    assert _poll(store, monkeypatch) == 0


def test_the_second_poll_only_reads_what_changed(store, monkeypatch):
    make_person("dev", "Dev Shah", handles=["dev@example.com"])
    inbox = store / "V10" / "acct" / "INBOX.mbox" / "Messages"
    write_emlx(inbox, "1.emlx", mail_from("dev@example.com", "first"))

    assert _poll(store, monkeypatch) == 1
    cursor = db.get_sync_state(applemail.CURSOR_KEY)
    assert cursor, "a high-water mark was recorded"

    # Nothing new: nothing parsed, nothing written.
    assert _poll(store, monkeypatch) == 0

    write_emlx(inbox, "2.emlx", mail_from("dev@example.com", "second"),
               mtime=time.time() + 10)
    assert _poll(store, monkeypatch) == 1


def test_an_attachment_is_parsed_from_the_file_itself(store, monkeypatch):
    """No fetching: the bytes were always in the message."""
    make_person("school", "Brightwood", handles=["office@school.example"])
    message = mail_from("office@school.example", "the packet", "see attached")
    message.add_attachment(b"BEGIN:VCALENDAR\nEND:VCALENDAR\n",
                           maintype="text", subtype="calendar",
                           filename="invite.ics")
    inbox = store / "V10" / "acct" / "INBOX.mbox" / "Messages"
    write_emlx(inbox, "1.emlx", message)

    assert _poll(store, monkeypatch) == 1
    stored = db.messages_since("2000-01-01", source="mail")[0]
    rows = db.attachments_for_message(stored.id)
    assert len(rows) == 1 and rows[0].filename == "invite.ics"
    assert rows[0].source == "mail"


def test_the_account_is_learned_from_the_sent_folder(store, monkeypatch):
    """Whose mailbox this is, without parsing a preferences plist whose shape
    changes with every macOS release."""
    sent = store / "V10" / "acct" / "Sent Messages.mbox" / "Messages"
    for i in range(3):
        write_emlx(sent, f"{i}.emlx",
                   mail_from("Alex <alex.carter@gmail.com>", f"note {i}",
                             to="someone@example.com"))
    monkeypatch.setattr(applemail, "MAIL_ROOT", store)

    assert applemail.detect_account(store / "V10") == "alex.carter@gmail.com"
    assert db.get_sync_state(applemail.ACCOUNT_KEY) == "alex.carter@gmail.com"
