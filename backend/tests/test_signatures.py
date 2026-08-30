"""Signature identity harvesting (§v1.4): a sender's own signature links their
phone to their person — prospectively, and retroactively for unsaved numbers
(the Katie case)."""
from __future__ import annotations

from datetime import timedelta

from lifeline import db
from lifeline.ingestion import signatures
from lifeline.models import Message, new_id

from tests.conftest import NOW, make_person, make_conversation

EMAIL = """Hi Alex,

Great speaking with you today about the Field Deployment Engineer role.
Let me know your availability for next week.

Best,
Katie Marsh
Recruiter, Meridian Labs
katie.marsh@meridian-labs.com
(347) 555-0179
"""


def test_signature_links_phone_to_sender():
    katie = make_person("katie-marsh", "Katie Marsh", relationship=None,
                        handles=["katie.marsh@meridian-labs.com"])
    linked = signatures.harvest(katie, EMAIL)
    assert linked == ["+13475550179"]
    assert "+13475550179" in db.get_person("katie-marsh").handles

    # Provenance lands in the model of you.
    facts = db.list_facts(subject_type="person", subject_id="katie-marsh")
    assert any("+13475550179" in f.statement and f.source == "derived" for f in facts)

    # Idempotent.
    assert signatures.harvest(katie, EMAIL) == []


def test_orphan_number_person_is_merged_the_katie_case():
    """She texted from an unsaved number; her signature identifies it."""
    orphan = make_person("13475550179", "+13475550179", relationship=None,
                         handles=["+13475550179"])
    make_conversation("imessage:+13475550179", name="+13475550179")
    db.insert_messages([
        Message(id=new_id(), source="imessage", conversation_id="imessage:+13475550179",
                external_id=new_id(), person_id=orphan.id, is_from_user=False,
                timestamp=(NOW - timedelta(days=1)).isoformat(),
                text="Hi, it's Katie — following up on the role!"),
    ])

    katie = make_person("katie-marsh", "Katie Marsh", relationship=None,
                        handles=["katie.marsh@meridian-labs.com"])
    linked = signatures.harvest(katie, EMAIL)
    assert linked == ["+13475550179"]

    # Proposed, not enacted (audit finding #7): the old merge DELETEd the
    # orphan and left four tables pointing at the dead id. The orphan row
    # stands; the claim is an alias plus a same_person_as fact, and the
    # number already *resolves* to Katie through the entity layer.
    from lifeline import world

    assert db.get_person("13475550179") is not None, "nothing was deleted"
    assert world.resolve("+13475550179").id == "katie-marsh"
    facts = db.get_connection().execute(
        "SELECT predicate, value FROM facts WHERE subject_id = 'katie-marsh' "
        "AND predicate = 'same_person_as'"
    ).fetchall()
    assert [f["value"] for f in facts] == ["13475550179"]


def test_named_owner_is_never_stolen():
    """If a *named* person already has the number, ambiguity wins — no link."""
    make_person("tanya-brooks", "Tanya Brooks", relationship=None, handles=["+13475550179"])
    katie = make_person("katie-marsh", "Katie Marsh", relationship=None,
                        handles=["katie.marsh@meridian-labs.com"])
    assert signatures.harvest(katie, EMAIL) == []
    assert "+13475550179" not in db.get_person("katie-marsh").handles
    assert "+13475550179" in db.get_person("tanya-brooks").handles


def test_body_numbers_outside_tail_ignored():
    katie = make_person("katie-marsh", "Katie Marsh", relationship=None,
                        handles=["katie.marsh@meridian-labs.com"])
    body = "Your order total is 347 555 0122 cents.\n" + "\n" * 20 + "Best,\nKatie"
    assert signatures.harvest(katie, body) == []


def test_reference_numbers_and_quoted_footers_are_not_cells():
    """Audit finding #7's other half: 37 linked numbers, all corporate footers
    or reference numbers — "+426865774900 is Capital One's number" was a fact.
    A 12-digit run is not a phone, and a quoted reply chain is not the
    sender's signature."""
    person = make_person("cap", "Capital One Support", relationship=None, handles=[])

    assert signatures.harvest(person, "Thanks!\n\nReference: 42 686 577 4900\n") == []

    quoted = (
        "Sounds good, talk soon\n"
        "\n"
        "On Aug 12, 2026, at 3:04 PM, Billing <billing@corp.com> wrote:\n"
        "> Questions? Call us at (800) 555-0199\n"
        "> Capital One, P.O. Box 30285\n"
    )
    assert signatures.harvest(person, quoted) == [], "someone else's footer"
