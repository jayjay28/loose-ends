"""Apple Mail ingestion (§v3) — mail through the door that needs no permission.

The Gmail API costs a Google Cloud project, an OAuth client, a consent screen
and three console clicks the wizard can only *point* at — the single worst
stretch of onboarding, and the one that has ended two real walkthroughs. But
if the person has their mail in Apple Mail, macOS already keeps every message
on disk, and the Full Disk Access grant that unlocks ``chat.db`` unlocks it
too. Same bargain ``applecal.py`` struck for the calendar: read the local
store the OS already maintains.

**Files, not the index.** Mail also keeps a SQLite ``Envelope Index``, and
reading it would be faster — but its schema is undocumented, differs across
macOS versions, and would be a third undocumented schema to nurse. The
``.emlx`` files beside it are just RFC-822 with a byte count on the front and
a plist on the back, which ``email.parser`` has understood for twenty years.
Enumeration is a ``scandir`` walk filtered on mtime, so a poll only parses
what changed.

**One mapping, not two.** Records come out in the exact shape `mail.store`
consumes, so this door reuses the identity resolution, signature harvesting
and bulk-mail filtering the pipeline already had — it adds a way in, not a
second idea of what mail is.
"""
from __future__ import annotations

import email
import logging
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.message import Message as EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .. import db
from . import attachments, mail
from .base import IdentityResolver

log = logging.getLogger(__name__)

MAIL_ROOT = Path.home() / "Library" / "Mail"
CURSOR_KEY = "applemail:mtime"
ACCOUNT_KEY = "applemail:account"

# How far back the first run reaches.
BACKFILL_DAYS = mail.BACKFILL_DAYS

# A body is a body; the rest is a wall of quoted history and legal boilerplate.
MAX_BODY = 20_000

# Mailboxes whose contents are mine rather than someone's message to me.
_SENT_MAILBOX = re.compile(r"/(Sent Messages|Sent|Drafts)\.mbox/", re.I)
# Never worth reading: the deleted, the junk, and Mail's own scratch copies.
_SKIP_MAILBOX = re.compile(r"/(Trash|Deleted Messages|Junk|Spam)\.mbox/", re.I)


def store_root() -> Optional[Path]:
    """The newest ``V*`` directory Mail keeps, or None when there isn't one.

    The version climbs with macOS (V2 … V10 and onward) and old ones are left
    behind, so the highest number is the live one.

    Never raises. Without Full Disk Access this directory *exists* and listing
    it throws `PermissionError` rather than coming back empty — the same trap
    `chat.db` sets — so every caller here would explode at exactly the moment
    the wizard is trying to tell the user what's missing. Use
    `permission_denied()` to tell "no Mail" from "no permission"."""
    try:
        if not MAIL_ROOT.is_dir():
            return None
        versions = []
        for child in MAIL_ROOT.iterdir():
            match = re.fullmatch(r"V(\d+)", child.name)
            if match and child.is_dir():
                versions.append((int(match.group(1)), child))
    except OSError:
        return None
    if not versions:
        return None
    return max(versions)[1]


def permission_denied() -> bool:
    """Mail's store is there, and macOS won't let this process look inside.

    The actionable half of "no mail found": the answer is Full Disk Access,
    not "set up Mail"."""
    try:
        MAIL_ROOT.is_dir() and list(MAIL_ROOT.iterdir())
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def available() -> bool:
    """Whether this Mac has mail worth reading. The setup wizard asks, so it
    can tell the difference between "no mail yet" and "Mail isn't set up"."""
    root = store_root()
    if root is None:
        return False
    try:
        for _ in _walk(root, newer_than=None, limit=1):
            return True
    except OSError:
        return False
    return False


# ------------------------------------------------------------------ walking
def _walk(root: Path, newer_than: Optional[float],
          limit: Optional[int] = None) -> Iterator[Tuple[Path, float]]:
    """Every message file worth parsing, newest work first.

    ``os.walk`` rather than ``rglob``: a large mailbox is tens of thousands of
    files, and this filters on the cheap ``stat`` before anything is opened.
    """
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
        # Mail's own indexes and caches hold no messages.
        if "MailData" in dirnames:
            dirnames.remove("MailData")
        if _SKIP_MAILBOX.search(dirpath + "/"):
            dirnames[:] = []
            continue
        for name in filenames:
            if not name.endswith(".emlx") or name.startswith("."):
                continue
            path = Path(dirpath) / name
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if newer_than is not None and mtime <= newer_than:
                continue
            yield path, mtime
            seen += 1
            if limit is not None and seen >= limit:
                return


def read_emlx(path: Path) -> Optional[EmailMessage]:
    """Parse one ``.emlx``: a byte count, the RFC-822 message, then Apple's
    plist. A ``.partial.emlx`` carries headers and no body — still worth
    having, since the subject and sender are most of an item.

    Never raises: a mailbox is a pile of other people's formatting, and one
    malformed file must not end a poll."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        first, rest = raw.split(b"\n", 1)
        length = int(first.strip())
        body = rest[:length]
    except (ValueError, IndexError):
        # No length prefix (or a truncated file) — treat the whole thing as
        # the message and let the parser take what it can.
        body = raw
    try:
        return email.message_from_bytes(body)
    except Exception:  # the email package raises a wide and shifting family
        log.debug("applemail: unparseable %s", path.name)
        return None


def _body_text(message: EmailMessage) -> str:
    """The readable part, plain text preferred, HTML flattened with the same
    noise-stripping the rest of the pipeline expects of a mail body."""
    plain, html = "", ""
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():          # an attachment, not the letter
            continue
        subtype = part.get_content_subtype()
        if subtype not in ("plain", "html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, ValueError):
            text = payload.decode("utf-8", errors="replace")
        if subtype == "plain" and not plain:
            plain = text
        elif subtype == "html" and not html:
            html = text
    if plain.strip():
        return plain.strip()[:MAX_BODY]
    if html:
        flat = mail._HTML_TAG.sub(" ", mail._HTML_NOISE.sub(" ", html))
        return re.sub(r"\s+", " ", flat).strip()[:MAX_BODY]
    return ""


def _attachments(message: EmailMessage) -> List[Dict[str, object]]:
    """What the message carries, metadata only. The bytes are already on
    disk; nothing is copied here."""
    found: List[Dict[str, object]] = []
    if not message.is_multipart():
        return found
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=False)
        found.append({
            "filename": filename,
            "mimeType": part.get_content_type(),
            "size": len(payload) if isinstance(payload, str) else 0,
        })
    return found


def normalise(message: EmailMessage, path: Path) -> Optional[dict]:
    """One parsed mail file -> the flat record shape `mail.store` consumes.

    The external id is the RFC-822 ``Message-ID``: canonical, stable across
    re-syncs, and the same string every other mail client would agree on.
    """
    def header(name: str) -> str:
        value = message.get(name, "")
        if not isinstance(value, str):
            value = str(value)
        return value.replace("\n", " ").strip()

    message_id = header("Message-ID").strip("<>")
    if not message_id:
        # No Message-ID (rare, but drafts and some senders skip it): fall back
        # to the file's own identity so re-polls still converge on one row.
        message_id = f"path:{path.name}"

    date_header = header("Date")
    try:
        when = parsedate_to_datetime(date_header).astimezone(timezone.utc)
    except (TypeError, ValueError):
        try:
            when = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            when = datetime.now(timezone.utc)

    subject = header("Subject")
    body = _body_text(message)
    if not subject and not body:
        return None

    # Threading: the References chain names the conversation's first message,
    # which is exactly what Gmail's threadId stands for.
    references = header("References").split()
    thread_root = (references[0].strip("<>") if references
                   else header("In-Reply-To").strip("<>") or message_id)

    return {
        "id": f"applemail:{message_id}",
        "threadId": thread_root,
        "from": header("From"),
        "to": header("To"),
        "subject": subject,
        "date": when.isoformat(timespec="seconds"),
        "labelIds": [],
        "snippet": body[:200],
        "body": body,
        "attachments": _attachments(message),
        # The same bulk-mail markers the pipeline already filters on, read
        # straight off the headers rather than out of a label vocabulary.
        "list_unsubscribe": bool(header("List-Unsubscribe")),
        "precedence": header("Precedence").lower(),
        "auto_submitted": header("Auto-Submitted").lower(),
        # Which mailbox the file sat in: Mail's answer to "is this in the
        # inbox", and to "did I write it".
        "in_inbox": not _SENT_MAILBOX.search(str(path)),
        "_sent": bool(_SENT_MAILBOX.search(str(path))),
    }


# -------------------------------------------------------------- the account
def detect_account(root: Path) -> str:
    """Whose mailbox this is, learned from the Sent folder rather than from a
    preferences plist: the address that sends the mail in Sent is the user's,
    whatever Mail's settings file happens to be shaped like this year."""
    cached = db.get_sync_state(ACCOUNT_KEY)
    if cached:
        return cached
    senders: Counter = Counter()
    for path, _ in _walk(root, newer_than=None, limit=400):
        if not _SENT_MAILBOX.search(str(path)):
            continue
        message = read_emlx(path)
        if message is None:
            continue
        _, address = mail.parse_from(str(message.get("From", "")))
        if address:
            senders[address] += 1
        if sum(senders.values()) >= 25:
            break
    if not senders:
        return ""
    account = senders.most_common(1)[0][0]
    db.set_sync_state(ACCOUNT_KEY, account)
    return account

# ------------------------------------------------------------------- poller
def poll(resolver: Optional[IdentityResolver] = None,
         root: Optional[Path] = None) -> int:
    """Ingest new local mail. Returns rows written.

    Incremental on file mtime: the first run reaches back `BACKFILL_DAYS`, and
    every run after only parses files touched since the last one. A failure of
    any kind is a no-op that says its name — the store belongs to Mail, and
    being unreadable for a while is a thing it is allowed to do.
    """
    base = Path(root) if root else store_root()
    if base is None:
        return 0

    cursor = db.get_sync_state(CURSOR_KEY)
    if cursor:
        try:
            newer_than: Optional[float] = float(cursor)
        except ValueError:
            newer_than = None
    else:
        newer_than = (datetime.now(timezone.utc)
                      - timedelta(days=BACKFILL_DAYS)).timestamp()

    account = detect_account(base)
    records: List[dict] = []
    carriers: Dict[str, Path] = {}     # record id -> file, for the ones with files
    high_water = newer_than or 0.0

    try:
        for path, mtime in _walk(base, newer_than):
            high_water = max(high_water, mtime)
            message = read_emlx(path)
            if message is None:
                continue
            record = normalise(message, path)
            if record is None:
                continue
            _, sender = mail.parse_from(record.get("from", ""))
            sent_by_me = record.pop("_sent", False) or (
                bool(account) and sender == account)
            # Keep everything I sent — the completion engine reads my own
            # replies as evidence — and keep inbound mail only when a person
            # rather than a robot sent it.
            if sent_by_me or mail.is_primary_inbound(record):
                records.append(record)
                if record["attachments"]:
                    carriers[record["id"]] = path
    except OSError as exc:
        log.warning("applemail poll no-op: %s: %s", type(exc).__name__, exc)
        return 0

    written = mail.store(records, account_email=account, resolver=resolver)

    # Read what the new mail carried. Its failure must not cost the poll —
    # the messages are already stored.
    for record_id, path in carriers.items():
        try:
            row = db.get_message_by_external_id(mail.SOURCE, record_id)
            parsed = read_emlx(path)
            if row is not None and parsed is not None:
                attachments.ingest_email(row, parsed)
        except Exception:
            log.exception("attachment ingest failed for %s", path.name)

    if high_water:
        db.set_sync_state(CURSOR_KEY, repr(high_water))
    return written
