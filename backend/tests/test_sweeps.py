"""Silence detection (§v1.4 pillar A): the first idle sweep — you spoke last,
nobody answered, the system notices the open loop for you.

§v2 step 1: what it produces is now a **thread**, not an information item.
"X went quiet" is an open loop the user is carrying, so it belongs on the
stack rather than in the proposals view an item would have become."""
from __future__ import annotations

from datetime import timedelta

from lifeline import db, threads
from lifeline.assistant import sweeps
from lifeline.models import Message, ThreadOrigin, ThreadState, new_id

from tests.conftest import NOW, make_conversation, make_item, make_person


def say(conversation_id, person_id, text, days_ago, from_user=False):
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    db.insert_messages(
        [
            Message(
                id=new_id(),
                source="imessage",
                conversation_id=conversation_id,
                external_id=new_id(),
                person_id=person_id,
                is_from_user=from_user,
                timestamp=ts,
                text=text,
            )
        ]
    )


def busy_tie(person_id, conversation_id):
    """Enough recent two-way traffic that the tie clears MIN_TIE."""
    for d in range(10, 40, 3):
        say(conversation_id, person_id, "hey", d, from_user=False)
        say(conversation_id, person_id, "yo", d, from_user=True)


def source_is_live(days_ago=0.0):
    """Somebody — anybody — sent something recently, which is the only proof
    the sweep has that the source is still delivering.

    Every test here used to run against a store whose newest inbound message
    was ten days old, and the sweep read that as nine days of one person's
    silence rather than ten days of nothing arriving at all. A real store has
    other conversations in it; this is them.
    """
    make_conversation("imessage:elsewhere", name="Someone Else")
    say("imessage:elsewhere", None, "dinner tomorrow?", days_ago)


def test_silence_opens_a_live_thread():
    make_person("ben", "Ben Cole", relationship=None)
    make_conversation("imessage:ben", name="Ben Cole")
    busy_tie("ben", "imessage:ben")
    source_is_live()
    say("imessage:ben", "ben", "any word on the work trial?", 9, from_user=True)

    created = sweeps.silence_sweep(NOW)
    assert created == 1

    stack = db.open_threads()
    assert len(stack) == 1
    thread = stack[0]
    # Live, not proposed: the sweep is v1.5's only proactive producer, and
    # routing it through an opt-in view would be a regression.
    assert thread.state == ThreadState.LIVE
    assert thread.origin == ThreadOrigin.SILENCE
    assert thread.title == "Ben Cole went quiet"
    assert "9 days" in thread.summary
    assert "work trial" in thread.summary

    # ... and it produces no item at all any more.
    assert db.open_items() == []

    # The silence itself is the founding evidence, and it's a message.
    evidence = db.thread_evidence(thread.id)
    assert len(evidence) == 1
    assert evidence[0].kind == "message"
    assert evidence[0].role == "founding"

    # Re-running doesn't duplicate while the thread is open.
    assert sweeps.silence_sweep(NOW) == 0
    assert len(db.open_threads()) == 1


def test_no_discovery_when_they_answered():
    make_person("ben", "Ben Cole", relationship=None)
    make_conversation("imessage:ben", name="Ben Cole")
    busy_tie("ben", "imessage:ben")
    source_is_live()
    say("imessage:ben", "ben", "any word?", 9, from_user=True)
    say("imessage:ben", "ben", "yes! starting Monday", 8, from_user=False)
    assert sweeps.silence_sweep(NOW) == 0


def test_fresh_silence_not_surfaced_yet():
    make_person("ben", "Ben Cole", relationship=None)
    make_conversation("imessage:ben", name="Ben Cole")
    busy_tie("ben", "imessage:ben")
    source_is_live()
    say("imessage:ben", "ben", "any word?", 1, from_user=True)   # only a day
    assert sweeps.silence_sweep(NOW) == 0


def test_unknown_numbers_and_weak_ties_skipped():
    source_is_live()

    # Unknown number: person whose display name has no letters.
    make_person("14045551212", "+14045551212", relationship=None)
    make_conversation("imessage:num", name="+14045551212")
    say("imessage:num", "14045551212", "who dis", 9, from_user=True)

    # Weak tie: a person with no history at all.
    make_person("stranger", "Sal Stranger", relationship=None)
    make_conversation("imessage:sal", name="Sal Stranger")
    say("imessage:sal", "stranger", "hello?", 9, from_user=True)

    assert sweeps.silence_sweep(NOW) == 0


def test_resolving_the_thread_stops_renudge_until_you_speak_again():
    make_person("ben", "Ben Cole", relationship=None)
    make_conversation("imessage:ben", name="Ben Cole")
    busy_tie("ben", "imessage:ben")
    source_is_live()
    say("imessage:ben", "ben", "any word on the trial?", 9, from_user=True)

    assert sweeps.silence_sweep(NOW) == 1
    thread = db.open_threads()[0]
    threads.resolve(thread.id, by="user")
    assert sweeps.silence_sweep(NOW) == 0  # you dealt with it; it stays quiet


def test_a_new_silence_after_you_speak_again_opens_a_new_thread():
    """The dedupe key is the *silence*, not the conversation — so a loop you
    already closed can't block the next one."""
    make_person("ben", "Ben Cole", relationship=None)
    make_conversation("imessage:ben", name="Ben Cole")
    busy_tie("ben", "imessage:ben")
    source_is_live()
    say("imessage:ben", "ben", "any word on the trial?", 20, from_user=True)

    assert sweeps.silence_sweep(NOW) == 1
    threads.resolve(db.open_threads()[0].id, by="user")
    assert sweeps.silence_sweep(NOW) == 0

    # You reach out again and are met with silence again.
    say("imessage:ben", "ben", "circling back on this", 9, from_user=True)
    assert sweeps.silence_sweep(NOW) == 1
    assert len(db.open_threads()) == 1


def test_an_open_silence_thread_blocks_a_second_one():
    """Same conversation, still unanswered: one nudge, not one per message."""
    make_person("ben", "Ben Cole", relationship=None)
    make_conversation("imessage:ben", name="Ben Cole")
    busy_tie("ben", "imessage:ben")
    source_is_live()
    say("imessage:ben", "ben", "any word on the trial?", 20, from_user=True)
    assert sweeps.silence_sweep(NOW) == 1

    say("imessage:ben", "ben", "still waiting", 9, from_user=True)
    assert sweeps.silence_sweep(NOW) == 0
    assert len(db.open_threads()) == 1


def test_a_dead_source_does_not_manufacture_silence():
    """The bug this gate exists for. iMessage stopped ingesting on 2026-08-22
    and every conversation in it went quiet four days later on schedule —
    three of the four live silence threads on the real database were opened
    about people who, for all the system could see, may well have replied."""
    make_person("ben", "Ben Cole", relationship=None)
    make_conversation("imessage:ben", name="Ben Cole")
    busy_tie("ben", "imessage:ben")
    say("imessage:ben", "ben", "any word on the work trial?", 9, from_user=True)
    source_is_live(days_ago=6)      # nothing has arrived from anyone in six days

    assert sweeps.silence_sweep(NOW) == 0
    assert db.open_threads() == []

    # The moment the pipe is open again, the same silence is real and surfaces.
    source_is_live()
    assert sweeps.silence_sweep(NOW) == 1


def test_silence_is_counted_from_what_the_source_last_saw():
    """Not from the clock. If the store went dark for two days, the user has
    been waiting for nine days and the system has only witnessed seven of
    them — and seven is the number it is entitled to say out loud."""
    make_person("ben", "Ben Cole", relationship=None)
    make_conversation("imessage:ben", name="Ben Cole")
    busy_tie("ben", "imessage:ben")
    say("imessage:ben", "ben", "any word on the work trial?", 9, from_user=True)
    source_is_live(days_ago=2)

    assert sweeps.silence_sweep(NOW) == 1
    thread = db.open_threads()[0]
    assert "7 days" in thread.summary
    assert "9 days" not in thread.summary


def test_a_tapback_is_not_a_last_word():
    """"Liked "OK, we'll talk later"" founded a live thread claiming Theo B
    went quiet — while his actual reply sat one row up. A reaction closes an
    exchange; it does not open a loop."""
    make_person("ben", "Ben Cole", relationship=None)
    make_conversation("imessage:ben", name="Ben Cole")
    busy_tie("ben", "imessage:ben")
    source_is_live()
    say("imessage:ben", "ben", "Liked “OK, we'll talk later”", 9, from_user=True)

    assert sweeps.silence_sweep(NOW) == 0


# ------------------------------------------- proposing what extraction found
def test_a_strong_unattached_item_is_offered_as_a_loop():
    """The gap the audit found: 541 open items against one new thread, because
    extraction filled the items table and nothing connected it to the stack the
    app actually shows."""
    from lifeline.models import ThreadState

    make_conversation()
    make_person("tess", "Tess", "spouse")
    item = make_item(text="can you send the deposit before friday")
    db.get_connection().execute(
        "UPDATE items SET score = 0.9, status = 'pending' WHERE id = ?", (item.id,))
    db.get_connection().commit()

    assert sweeps.propose_sweep() == 1
    proposed = db.list_threads(states=[ThreadState.PROPOSED])
    assert len(proposed) == 1
    assert db.thread_evidence(proposed[0].id)[0].ref_id == item.id


def test_a_weak_item_is_not_offered():
    """Proposing guesses teaches people to ignore the proposals."""
    make_conversation()
    make_person("tess", "Tess", "spouse")
    item = make_item(text="ok")
    db.get_connection().execute(
        "UPDATE items SET score = 0.2, status = 'pending' WHERE id = ?", (item.id,))
    db.get_connection().commit()
    assert sweeps.propose_sweep() == 0


def test_an_item_already_claimed_is_not_offered_again():
    from lifeline.models import ThreadState

    make_conversation()
    make_person("tess", "Tess", "spouse")
    item = make_item(text="can you send the deposit before friday")
    db.get_connection().execute(
        "UPDATE items SET score = 0.9, status = 'pending' WHERE id = ?", (item.id,))
    db.get_connection().commit()

    assert sweeps.propose_sweep() == 1
    assert sweeps.propose_sweep() == 0, "one loop per item, however often we sweep"
    assert len(db.list_threads(states=[ThreadState.PROPOSED])) == 1


def test_a_cycle_offers_a_readable_number_not_an_inbox():
    make_conversation()
    make_person("tess", "Tess", "spouse")
    for n in range(12):
        item = make_item(text=f"please send thing number {n} before friday")
        db.get_connection().execute(
            "UPDATE items SET score = 0.9, status = 'pending' WHERE id = ?", (item.id,))
    db.get_connection().commit()
    assert sweeps.propose_sweep() == sweeps.PROPOSE_PER_CYCLE
