"""Email-signature identity harvesting (§v1.4, cross-channel-state.md).

The highest-precision identity signal we have: a person's own email signature
listing their cell. Harvesting it links their phone — and therefore their
iMessage thread — to the same person as their email, so a text can close a
loop that arrived by mail.

Two effects, both provenance-tracked as derived facts:
- prospective: the number joins the person's handles, so future texts resolve.
- retroactive: if an *orphan* person already exists for that bare number
  (unsaved contact — display name is just the number), it is merged in and its
  messages re-pointed. This is exactly the Katie case.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

from .. import db
from ..models import Fact, Person
from .base import normalise_handle

log = logging.getLogger(__name__)

# +1 347-555-0179 / (347) 555-0179 / 347.555.0179 / +44 20 7946 0958
_PHONE = re.compile(
    r"(?:(?<=\s)|^)(\+?\d{1,3}[\s.-]?)?(\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?=\D|$)"
)
_TAIL_LINES = 12          # signatures live at the bottom
_MAX_HANDLES = 5          # a person with more is probably a list/bot


# The reply chain is other people's text. A quoted third-party number in a
# forwarded footer used to become "the sender's cell" — the audit counted 37
# linked numbers and every one was a corporate footer or a reference number.
_QUOTE_MARKERS = re.compile(r"\n\s*>|\nOn .{0,80} wrote:", re.S)


def harvest(person: Person, text: str) -> List[str]:
    """Scan an inbound email's tail for the sender's phone number(s) and link
    them to the sender. Returns the handles that were newly linked.

    Precision over recall, twice (audit finding #7): only the sender's own
    words — everything after the first quote marker is someone else's mail —
    and only NANP-shaped numbers. A 12-digit run is a reference number; the
    live store had "+426865774900 is Capital One's number" filed as a fact.
    """
    if not text:
        return []
    own = _QUOTE_MARKERS.split(text)[0]
    tail = "\n".join(own.strip().splitlines()[-_TAIL_LINES:])
    linked: List[str] = []
    for m in _PHONE.finditer(tail):
        raw = m.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            continue          # not a NANP cell — a reference number, or abroad
        handle = "+1" + digits
        if _link(person, handle):
            linked.append(handle)
    return linked


def _link(person: Person, handle: str) -> bool:
    key = normalise_handle(handle)
    if any(normalise_handle(h) == key for h in person.handles):
        return False  # already theirs
    if len(person.handles) >= _MAX_HANDLES:
        return False

    owner = _handle_owner(key, exclude=person.id)
    if owner is not None:
        if _is_orphan(owner):
            _propose_merge(owner, person)  # the unsaved-number case
            return True
        return False                       # a *named* person owns it — ambiguous, leave it

    person.handles.append(handle)
    db.upsert_person(person)
    _note(person, handle, "signature")
    log.info("signature: linked %s to %s", handle, person.id)
    return True


def _handle_owner(key: str, exclude: str) -> Optional[Person]:
    for p in db.list_people():
        if p.id != exclude and any(normalise_handle(h) == key for h in p.handles):
            return p
    return None


def _is_orphan(person: Person) -> bool:
    """A person that exists only as a bare number (nobody saved the contact)."""
    return not any(ch.isalpha() for ch in person.display_name)


def _propose_merge(orphan: Person, person: Person) -> None:
    """Same person, two rows — claimed, not enacted (audit finding #7).

    The old `_merge` rewrote three tables, DELETEd the orphan, and left four
    other referrers (threads.contact_person_id, behavior_events, model
    weights, watcher specs) pointing at a dead id, with no way back. It never
    fired in production; the 37 junk signature links were the warning.

    Now the claim is recorded where the rest of the system can act on it: an
    entity alias (so resolution already treats the number as this person) and
    a `same_person_as` fact with signature-grade confidence. Enacting a full
    merge — every referrer, reversibly — is the entity layer's job, done
    deliberately, not a side effect of parsing an email footer.
    """
    from .. import world

    for handle in orphan.handles:
        world.reassign_alias(handle, person.id, source="signature")
    db.upsert_fact(
        Fact(
            subject_type="person",
            subject_id=person.id,
            statement=(f"{orphan.display_name} ({orphan.id}) appears to be the same "
                       f"person — their number is in {person.display_name}'s signature"),
            predicate="same_person_as",
            value=orphan.id,
            source="derived",
            confidence=0.6,
            provenance="signature",
        )
    )
    log.info("signature: proposed merge of orphan %s into %s", orphan.id, person.id)


def _note(person: Person, handle: str, kind: str) -> None:
    """Provenance in the model of you — inspectable, dismissable."""
    db.upsert_fact(
        Fact(
            subject_type="person",
            subject_id=person.id,
            statement=f"{handle} is {person.display_name}'s number (from their email {kind.replace('-', ' ')})",
            source="derived",
            confidence=0.95,
            provenance=kind,
        )
    )
