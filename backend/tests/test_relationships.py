"""Tie strength must not overclaim on thin evidence (§v1.5).

The live bug: a recruiter with ONE inbound email and one reply scored 0.695
— "one of your closest — you two go back and forth constantly". Perfect
reciprocity on a sample of two.
"""
from __future__ import annotations

from datetime import timedelta

from lifeline import db
from lifeline.models import Message, new_id
from lifeline.ranking import relationships

from tests.conftest import NOW, make_person, make_conversation


def exchange(conversation_id, person_id, pairs, days_ago=1):
    """`pairs` rounds of (them, you) in one thread."""
    msgs = []
    for n in range(pairs):
        ts = (NOW - timedelta(days=days_ago, minutes=n)).isoformat()
        msgs.append(Message(id=new_id(), source="gmail", conversation_id=conversation_id,
                            external_id=new_id(), person_id=person_id,
                            is_from_user=False, timestamp=ts, text="hi"))
        msgs.append(Message(id=new_id(), source="gmail", conversation_id=conversation_id,
                            external_id=new_id(), person_id=None,
                            is_from_user=True, timestamp=ts, text="hey"))
    db.insert_messages(msgs)


def test_single_exchange_is_not_a_close_relationship():
    make_person("recruiter", "Katie Marsh", relationship=None)
    make_conversation("gmail:recruiter", source="gmail", name="Katie Marsh")
    exchange("gmail:recruiter", "recruiter", pairs=1)

    score = relationships.strengths(NOW, force=True)["recruiter"]
    assert score < 0.2, f"one exchange scored {score}"
    assert relationships.describe("recruiter", NOW) == "little history together"


def test_long_thread_still_reads_as_close():
    make_person("bestie", "Tam", relationship=None)
    make_conversation("imessage:bestie", name="Tam")
    exchange("imessage:bestie", "bestie", pairs=60)

    score = relationships.strengths(NOW, force=True)["bestie"]
    assert score >= 0.66, f"a 120-message thread scored {score}"
    assert "closest" in relationships.describe("bestie", NOW)


def test_confidence_grows_with_history():
    """The same perfect reciprocity should score higher with more evidence."""
    make_person("thin", "Thin", relationship=None)
    make_conversation("imessage:thin", name="Thin")
    exchange("imessage:thin", "thin", pairs=2)

    make_person("thick", "Thick", relationship=None)
    make_conversation("imessage:thick", name="Thick")
    exchange("imessage:thick", "thick", pairs=20)

    scores = relationships.strengths(NOW, force=True)
    assert scores["thin"] < scores["thick"]
