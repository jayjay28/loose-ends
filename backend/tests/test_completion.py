"""Completion detection (§7)."""
from __future__ import annotations

from datetime import timedelta

from conftest import NOW, days_from_now, make_item, make_person, make_conversation

from lifeline import db
from lifeline.completion import engine, matcher
from lifeline.models import CalendarEvent, Message, new_id, now_iso
from lifeline.ranking import learning


def setup_people():
    make_conversation()
    make_person("tess", "Tess", "spouse")


def add_email(subject, body, at=None, labels=None, from_email="orders@shop.com"):
    message = Message(
        id=new_id(),
        source="gmail",
        conversation_id="gmail:t1",
        external_id=new_id(),
        person_id=None,
        is_from_user=False,
        timestamp=(at or (NOW + timedelta(days=1))).isoformat(timespec="seconds"),
        text=f"{subject}\n\n{body}",
        metadata={"subject": subject, "from_email": from_email, "labels": labels or []},
    )
    db.upsert_conversation(__import__("lifeline.models", fromlist=["Conversation"]).Conversation(id="gmail:t1", source="gmail", display_name="shop"))
    db.insert_messages([message])
    return message


# ----------------------------------------------------------- auto-close
def test_receipt_with_exact_product_name_auto_closes():
    setup_people()
    item = make_item(item_type="purchase", text="I want that Lemaire croissant bag")
    item.entities.item = "Lemaire croissant bag"
    db.save_item(item)
    add_email(
        "Your Lemaire order #LM-884213 is confirmed",
        "Thank you for your order.\nCroissant Bag, Nubuck — $1,290.00",
        labels=["CATEGORY_PURCHASES"],
    )
    outcome = engine.scan(NOW)
    assert [i.id for i in outcome.auto_closed] == [item.id]
    closed = db.get_item(item.id)
    assert closed.status == "completed" and closed.completed_by == "auto"


def test_the_closing_signal_is_logged():
    setup_people()
    item = make_item(item_type="purchase", text="I want that Lemaire croissant bag")
    item.entities.item = "Lemaire croissant bag"
    db.save_item(item)
    add_email("Your Lemaire order is confirmed", "Croissant Bag — $1,290.00", labels=["CATEGORY_PURCHASES"])
    engine.scan(NOW)
    signals = db.signals_for_item(item.id)
    assert signals and signals[0].resolution == "auto_closed"
    assert signals[0].reasons, "the user must be able to see what closed it"


def test_unrelated_receipt_does_not_close_anything():
    setup_people()
    item = make_item(item_type="purchase", text="I want that Lemaire croissant bag")
    item.entities.item = "Lemaire croissant bag"
    db.save_item(item)
    add_email("Your Wirecutter order is confirmed", "Cast Iron Skillet — $39", labels=["CATEGORY_PURCHASES"])
    outcome = engine.scan(NOW)
    assert not outcome.auto_closed
    assert db.get_item(item.id).status == "pending"


def test_cancellation_language_blocks_a_match():
    setup_people()
    item = make_item(item_type="purchase", text="I want that Lemaire croissant bag")
    item.entities.item = "Lemaire croissant bag"
    db.save_item(item)
    add_email("Your Lemaire order was cancelled", "Croissant Bag — refunded", labels=["CATEGORY_PURCHASES"])
    assert not engine.scan(NOW).auto_closed


def test_evidence_predating_the_item_is_ignored():
    setup_people()
    item = make_item(item_type="purchase", text="I want that Lemaire croissant bag")
    item.entities.item = "Lemaire croissant bag"
    db.save_item(item)
    add_email(
        "Your Lemaire order is confirmed",
        "Croissant Bag — $1,290.00",
        at=NOW - timedelta(days=5),
        labels=["CATEGORY_PURCHASES"],
    )
    assert not engine.scan(NOW).auto_closed


def test_followups_may_be_closed_by_earlier_evidence():
    """"did you ever book the flights?" can be answered by yesterday's receipt."""
    setup_people()
    item = make_item(item_type="followup", text="did you ever book the flights for grandma's thing")
    item.entities.item = "book the flights"
    db.save_item(item)
    add_email(
        "Your trip to Los Angeles is confirmed — Confirmation UQXBTR",
        "Departing SFO to LAX on August 3. Alaska Airlines itinerary.",
        at=NOW - timedelta(hours=6),
    )
    outcome = engine.scan(NOW)
    assert outcome.auto_closed or outcome.needs_confirmation, "the itinerary is evidence either way"


def test_past_calendar_event_closes_the_item():
    setup_people()
    item = make_item(item_type="event", text="you still up for the Marin ride Sunday", at=NOW - timedelta(days=7))
    item.entities.item = "Marin ride"
    db.save_item(item)
    db.upsert_calendar_events(
        [
            CalendarEvent(
                id="cal-1",
                calendar_id="primary",
                summary="Marin ride w/ Theo",
                start_at=days_from_now(-1),
                end_at=days_from_now(-0.8),
                self_response="accepted",
            )
        ]
    )
    assert engine.scan(NOW).auto_closed


def test_cancelled_calendar_event_is_not_evidence():
    setup_people()
    item = make_item(item_type="event", text="the Marin ride")
    item.entities.item = "Marin ride"
    db.save_item(item)
    db.upsert_calendar_events(
        [
            CalendarEvent(
                id="cal-1",
                calendar_id="primary",
                summary="Marin ride w/ Theo",
                start_at=days_from_now(-1),
                end_at=days_from_now(-0.8),
                status="cancelled",
            )
        ]
    )
    assert not engine.scan(NOW).auto_closed


def test_future_event_never_closes_an_event_item():
    """Asking "did you finish Grandma's 80th?" before it happens is nonsense."""
    setup_people()
    item = make_item(item_type="event", text="Grandma's 80th is in 3 weeks")
    item.entities.item = "Grandma's 80th"
    db.save_item(item)
    db.upsert_calendar_events(
        [
            CalendarEvent(
                id="cal-1",
                calendar_id="primary",
                summary="Grandma's 80th",
                start_at=days_from_now(8),
                self_response="accepted",
            )
        ]
    )
    outcome = engine.scan(NOW)
    assert not outcome.auto_closed
    assert not outcome.needs_confirmation


def test_future_booking_can_close_a_promise():
    setup_people()
    item = make_item(item_type="promise", text="can you call the pediatrician about the rash")
    item.entities.item = "call the pediatrician"
    db.save_item(item)
    db.upsert_calendar_events(
        [
            CalendarEvent(
                id="cal-1",
                calendar_id="primary",
                summary="Pediatrician — Dr. Bell",
                start_at=days_from_now(2),
                self_response="accepted",
            )
        ]
    )
    outcome = engine.scan(NOW)
    assert outcome.auto_closed or outcome.needs_confirmation


# ------------------------------------------------------- fuzzy matching
def test_fuzzy_match_asks_instead_of_closing():
    setup_people()
    item = make_item(item_type="promise", text="can you call the pediatrician about Iris's rash")
    item.entities.item = "call the pediatrician about Iris's rash"
    db.save_item(item)
    db.upsert_calendar_events(
        [
            CalendarEvent(
                id="cal-1",
                calendar_id="primary",
                summary="Iris — Dr. Bell (pediatrics)",
                description="Rash follow-up.",
                start_at=days_from_now(2),
                self_response="accepted",
            )
        ]
    )
    outcome = engine.scan(NOW)
    assert not outcome.auto_closed
    assert len(outcome.needs_confirmation) == 1
    assert db.get_item(item.id).status == "pending"


def test_confirming_a_fuzzy_match_closes_the_item():
    setup_people()
    item = make_item(item_type="promise", text="can you call the pediatrician about Iris's rash")
    item.entities.item = "call the pediatrician about Iris's rash"
    db.save_item(item)
    db.upsert_calendar_events(
        [CalendarEvent(
            id="cal-1",
            calendar_id="primary",
            summary="Iris — Dr. Bell (pediatrics)",
            description="Rash follow-up.",
            start_at=days_from_now(2),
            self_response="accepted",
        )]
    )
    signal = engine.scan(NOW).needs_confirmation[0]
    closed = engine.confirm(signal.id)
    assert closed and closed.status == "completed"
    assert db.get_signal(signal.id).resolution == "confirmed"


def test_rejecting_a_fuzzy_match_keeps_the_item_open():
    setup_people()
    item = make_item(item_type="promise", text="can you call the pediatrician about Iris's rash")
    item.entities.item = "call the pediatrician about Iris's rash"
    db.save_item(item)
    db.upsert_calendar_events(
        [CalendarEvent(
            id="cal-1",
            calendar_id="primary",
            summary="Iris — Dr. Bell (pediatrics)",
            description="Rash follow-up.",
            start_at=days_from_now(2),
            self_response="accepted",
        )]
    )
    signal = engine.scan(NOW).needs_confirmation[0]
    engine.reject(signal.id)
    assert db.get_item(item.id).status == "pending"
    assert db.get_signal(signal.id).resolution == "rejected"
    assert not engine.open_confirmations()


def test_rejected_evidence_is_not_re_suggested():
    setup_people()
    item = make_item(item_type="promise", text="can you call the pediatrician about Iris's rash")
    item.entities.item = "call the pediatrician about Iris's rash"
    db.save_item(item)
    db.upsert_calendar_events(
        [CalendarEvent(
            id="cal-1",
            calendar_id="primary",
            summary="Iris — Dr. Bell (pediatrics)",
            description="Rash follow-up.",
            start_at=days_from_now(2),
            self_response="accepted",
        )]
    )
    signal = engine.scan(NOW).needs_confirmation[0]
    engine.reject(signal.id)
    assert not engine.scan(NOW).needs_confirmation


# ---------------------------------------------------------- manual path
def test_manual_close_works_and_teaches_the_model():
    setup_people()
    item = make_item(item_type="promise")
    engine.manual_close(item.id)
    closed = db.get_item(item.id)
    assert closed.status == "completed" and closed.completed_by == "manual"
    assert learning.manual_close_rate("promise") > 0.5


def test_manual_close_retires_an_open_confirmation():
    setup_people()
    item = make_item(item_type="promise", text="can you call the pediatrician about Iris's rash")
    item.entities.item = "call the pediatrician about Iris's rash"
    db.save_item(item)
    db.upsert_calendar_events(
        [CalendarEvent(
            id="cal-1",
            calendar_id="primary",
            summary="Iris — Dr. Bell (pediatrics)",
            description="Rash follow-up.",
            start_at=days_from_now(2),
            self_response="accepted",
        )]
    )
    engine.scan(NOW)
    engine.manual_close(item.id)
    assert not engine.open_confirmations()


# -------------------------------------------------------------- matcher
def test_generic_word_overlap_is_not_a_match():
    setup_people()
    item = make_item(text="I'll get the hoop for his birthday")
    item.entities.item = "the hoop for his birthday"
    # Only the generic word "birthday" is shared, so this must not match.
    # The haystack is kept lexically distant on purpose: `phrase_similarity`
    # on short strings sits near its own 0.6 threshold, and a haystack that
    # merely *looks* similar would pass this test through the fuzzy path
    # instead of the generic-word rule it exists to pin.
    overlap, _ = matcher.entity_overlap(item, "a birthday reminder from the calendar")
    assert overlap == 0.0


def test_category_bridge_links_flights_to_an_itinerary():
    setup_people()
    item = make_item(text="did you ever book the flights")
    assert matcher.category_bridge(item, "Departing SFO to LAX — Alaska itinerary")


def test_amount_agreement():
    setup_people()
    item = make_item(text="can you send me the $88 for the permit")
    assert matcher.amount_agreement(item, "Payment received: $88.00")
    assert matcher.amount_agreement(item, "Payment received: $12.00") is None


def test_reading_items_are_not_closed_by_email():
    setup_people()
    item = make_item(item_type="reading", text="found this article you'd love")
    item.entities.item = "article"
    db.save_item(item)
    add_email("Your article order is confirmed", "article — confirmed", labels=["CATEGORY_PURCHASES"])
    assert not engine.scan(NOW).auto_closed


def test_scan_is_idempotent():
    setup_people()
    item = make_item(item_type="promise", text="can you call the pediatrician about Iris's rash")
    item.entities.item = "call the pediatrician about Iris's rash"
    db.save_item(item)
    db.upsert_calendar_events(
        [CalendarEvent(
            id="cal-1",
            calendar_id="primary",
            summary="Iris — Dr. Bell (pediatrics)",
            description="Rash follow-up.",
            start_at=days_from_now(2),
            self_response="accepted",
        )]
    )
    engine.scan(NOW)
    engine.scan(NOW)
    assert len(db.signals_for_item(item.id)) == 1


# --------------------------------------------- the iMessage self-reply close
def _you_say(text, at, conversation_id="imessage:t1"):
    db.insert_messages([
        Message(
            id=new_id(),
            source="imessage",
            conversation_id=conversation_id,
            external_id=new_id(),
            person_id="tess",
            is_from_user=True,
            timestamp=at.isoformat(timespec="seconds"),
            text=text,
        )
    ])


def test_answering_a_question_closes_it():
    """§7's most productive closer: for a question, your own reply in the same
    conversation is the completion signal."""
    setup_people()
    item = make_item(item_type="question", text="what time is the game on Saturday")
    db.save_item(item)
    _you_say("2pm, I'll drive", NOW + timedelta(hours=2))

    engine.scan(NOW + timedelta(hours=3))
    assert db.get_item(item.id).status == "completed"


def test_promising_to_answer_does_not_close_it():
    """"Let me check and get back to you" is the loop being held open, in
    writing — and it was closing the item at 0.9 confidence, which then closed
    the thread that claimed it, because every item being settled is a thread's
    strongest closure signal."""
    setup_people()
    item = make_item(item_type="question", text="what time is the game on Saturday")
    db.save_item(item)
    _you_say("let me check and get back to you", NOW + timedelta(hours=2))

    engine.scan(NOW + timedelta(hours=3))
    assert db.get_item(item.id).status == "pending"

    # The actual answer, later, still closes it.
    _you_say("2pm", NOW + timedelta(hours=5))
    engine.scan(NOW + timedelta(hours=6))
    assert db.get_item(item.id).status == "completed"
