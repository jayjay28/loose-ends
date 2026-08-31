"""Importance ranking and interruption levels (§6)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import NOW, days_from_now, make_item, make_message, make_person, make_conversation

from lifeline import db
from lifeline.models import CalendarEvent, InterruptionLevel
from lifeline.ranking import behavior, learning, scorer, signals


def setup_people():
    make_conversation()
    make_person("tess", "Tess", "spouse")
    make_person("dev", "Dev Shah", "friend")


# --------------------------------------------------------------- levels
def test_imminent_deadline_is_time_sensitive():
    setup_people()
    item = make_item(date=days_from_now(1))
    scorer.score_item(item, NOW)
    assert item.interruption_level == InterruptionLevel.TIME_SENSITIVE


def test_distant_deadline_is_not_time_sensitive():
    setup_people()
    item = make_item(date=days_from_now(40))
    scorer.score_item(item, NOW)
    assert item.interruption_level != InterruptionLevel.TIME_SENSITIVE


def test_explicit_flexibility_makes_an_undated_item_passive():
    setup_people()
    item = make_item(text="no rush at all, whenever you get a sec — a dog sitter for October")
    scorer.score_item(item, NOW)
    assert item.interruption_level == InterruptionLevel.PASSIVE


def test_flexible_language_cannot_override_a_real_deadline():
    """"send it whenever" plus "by August 5" is still a dated obligation."""
    setup_people()
    item = make_item(text="can you send me the $88 whenever, it's due by August 5", date=days_from_now(9))
    scorer.score_item(item, NOW)
    assert item.interruption_level != InterruptionLevel.PASSIVE


def test_dependent_work_pulls_the_deadline_forward():
    """§6.1 — travel booking tied to an event date is due now, not on the day."""
    setup_people()
    db.upsert_calendar_events(
        [CalendarEvent(id="e1", calendar_id="primary", summary="Grandma's 80th", start_at=days_from_now(15))]
    )
    item = make_item(text="did you ever book the flights for grandma's 80th")
    item.entities.item = "book the flights for grandma's 80th"
    scorer.score_item(item, NOW)
    value, detail = signals.dependency_pressure(item, NOW)
    assert value >= 0.9, detail
    # No date of its own, yet it lands in Time-Sensitive because the event
    # it depends on is inside the booking lead time.
    assert item.entities.date is None
    assert item.interruption_level == InterruptionLevel.TIME_SENSITIVE


def test_dependency_pressure_decays_with_distance():
    setup_people()
    db.upsert_calendar_events(
        [CalendarEvent(id="e1", calendar_id="primary", summary="Grandma's 80th", start_at=days_from_now(40))]
    )
    item = make_item(text="book the flights for grandma's 80th")
    item.entities.item = "book the flights for grandma's 80th"
    near, _ = signals.dependency_pressure(item, NOW)
    assert 0 < near < 0.9


def test_dependency_ignores_generic_word_collisions():
    setup_people()
    db.upsert_calendar_events(
        [CalendarEvent(id="e1", calendar_id="primary", summary="Tess's birthday", start_at=days_from_now(10))]
    )
    item = make_item(text="I'll get the hoop for his birthday")
    item.entities.item = "the hoop for his birthday"
    value, _ = signals.dependency_pressure(item, NOW)
    assert value == 0.0, "a shared 'birthday' is not evidence of a dependency"


def test_ordering_is_by_level_then_score():
    setup_people()
    make_item(text="urgent thing", date=days_from_now(1))
    make_item(text="no rush whenever thing")
    make_item(text="middling thing", date=days_from_now(10))
    scorer.rescore_all(NOW)
    levels = [i.interruption_level for i in scorer.ranked(NOW)]
    assert levels == sorted(levels, key=lambda l: InterruptionLevel.ORDER[l])


def test_snoozed_items_are_hidden_until_they_wake():
    setup_people()
    item = make_item()
    item.status = "snoozed"
    item.snoozed_until = days_from_now(2)
    db.save_item(item)
    assert item.id not in {i.id for i in scorer.ranked(NOW)}
    assert item.id in {i.id for i in scorer.ranked(NOW, include_snoozed=True)}


def test_woken_snoozed_items_come_back():
    setup_people()
    item = make_item()
    item.status = "snoozed"
    item.snoozed_until = days_from_now(-1)
    db.save_item(item)
    assert item.id in {i.id for i in scorer.ranked(NOW)}


# -------------------------------------------------------------- signals
def test_starred_mail_raises_the_score():
    setup_people()
    plain = make_message("look at the paper", metadata={})
    starred = make_message("look at the paper", metadata={"starred": True})
    a = make_item(text="look at the paper", message_id=plain.id)
    b = make_item(text="look at the paper", message_id=starred.id)
    scorer.score_item(a, NOW)
    scorer.score_item(b, NOW)
    assert b.score > a.score


def test_promotional_mail_is_penalised():
    setup_people()
    promo = make_message("20% off power tools", metadata={"promotional": True})
    item = make_item(text="20% off power tools", message_id=promo.id)
    value, _ = signals.explicit_signals(item)
    assert value < 0


def test_sender_weight_prefers_learned_value_over_prior():
    setup_people()
    item = make_item(person_id="dev", person="Dev Shah")
    prior, _ = signals.sender_weight(item)
    db.set_weight("person:dev", 1.2, observations=10)
    learned, detail = signals.sender_weight(item)
    assert learned == 1.2 and learned != prior
    assert "learned" in detail


def test_reply_latency_measured_from_thread_history():
    setup_people()
    base = NOW - timedelta(days=3)
    make_message("hey", at=base, person_id="tess")
    make_message("hi", at=base + timedelta(minutes=20), is_from_user=True, person_id=None)
    make_message("and another", at=base + timedelta(hours=5), person_id="tess")
    make_message("yep", at=base + timedelta(hours=5, minutes=30), is_from_user=True, person_id=None)
    latencies = signals.person_reply_latencies("tess")
    assert len(latencies) == 2
    assert all(l < 1.5 for l in latencies)


def test_explanations_are_attached_and_ordered():
    setup_people()
    item = make_item(date=days_from_now(1))
    scorer.score_item(item, NOW)
    assert item.score_explanation
    contributions = [abs(e["contribution"]) for e in item.score_explanation]
    assert contributions == sorted(contributions, reverse=True)
    assert all({"signal", "detail", "contribution"} <= set(e) for e in item.score_explanation)


def test_time_of_day_favours_decisions_in_the_morning():
    """§6.4 — the window is the user's local mid-morning, not UTC."""
    setup_people()
    decision = make_item(item_type="promise")
    low_stakes = make_item(item_type="reading")
    morning = datetime(2026, 7, 27, 9, 0).astimezone()      # 9am local
    assert signals.time_of_day(decision, morning)[0] > 0
    assert signals.time_of_day(low_stakes, morning)[0] < 0


def test_time_of_day_defers_analytical_work_late_at_night():
    setup_people()
    decision = make_item(item_type="promise")
    late = datetime(2026, 7, 27, 23, 30).astimezone()
    assert signals.time_of_day(decision, late)[0] < 0


# ------------------------------------------------- avoidance vs. deprio
def test_repeated_views_with_social_stakes_read_as_avoidance():
    setup_people()
    item = make_item(text="can you call the pediatrician about the rash, I've asked twice")
    for _ in range(4):
        db.log_behavior("viewed", item_id=item.id)
    reading = behavior.classify(item, NOW)
    assert reading.is_avoidance, reading.evidence


def test_untouched_low_stakes_item_reads_as_deprioritized():
    setup_people()
    old = NOW - timedelta(days=30)
    item = make_item(item_type="reading", text="found this article you might like", at=old)
    # A history of ignoring reading items from everyone.
    for _ in range(4):
        make_item(item_type="reading", text="another link", at=old)
    reading = behavior.classify(item, NOW)
    assert reading.is_deprioritized, reading.evidence


def test_avoidance_boosts_score_and_deprioritization_sinks_it():
    setup_people()
    avoided = make_item(text="can you call the doctor to decide about the surgery")
    for _ in range(4):
        db.log_behavior("viewed", item_id=avoided.id)
    baseline = make_item(text="can you call the doctor to decide about the surgery")
    scorer.score_item(avoided, NOW)
    scorer.score_item(baseline, NOW)
    assert avoided.score > baseline.score
    assert avoided.behavior_pattern == "avoidance"


def test_avoidance_boost_is_gentle_not_dominant():
    """§6.3 — escalate gently; avoidance must not outrank a real deadline."""
    setup_people()
    avoided = make_item(text="can you call the doctor to decide about the surgery")
    for _ in range(5):
        db.log_behavior("viewed", item_id=avoided.id)
    urgent = make_item(text="rsvp needed", date=days_from_now(1))
    scorer.score_item(avoided, NOW)
    scorer.score_item(urgent, NOW)
    assert urgent.score > avoided.score


def test_a_single_weak_signal_is_not_a_diagnosis():
    setup_people()
    item = make_item()
    db.log_behavior("viewed", item_id=item.id)
    assert behavior.classify(item, NOW).pattern is None


# ------------------------------------------------------------ learning
def test_completion_raises_the_sender_weight():
    setup_people()
    item = make_item(person_id="dev", person="Dev Shah")
    before = db.get_weight("person:dev", 0.6)
    learning.record("completed_manual", item)
    assert db.get_weight("person:dev", 0.6) > before


def test_dismissal_lowers_the_pair_weight():
    setup_people()
    item = make_item(person_id="dev", person="Dev Shah", item_type="reading")
    learning.record("dismissed", item)
    assert db.get_weight("pair:dev/reading", 0.5) < 0.5


def test_manual_closes_teach_that_no_external_signal_exists():
    setup_people()
    item = make_item(item_type="promise")
    for _ in range(5):
        learning.record("completed_manual", make_item(item_type="promise"))
    assert learning.manual_close_rate("promise") > 0.6


def test_auto_closes_teach_the_opposite():
    setup_people()
    for _ in range(5):
        learning.record("completed_auto", make_item(item_type="purchase"))
    assert learning.manual_close_rate("purchase") < 0.4


def test_deprioritization_decays_weights_but_avoidance_does_not():
    setup_people()
    old = NOW - timedelta(days=30)
    for _ in range(4):
        make_item(item_type="reading", person_id="dev", person="Dev Shah", text="another link", at=old)
    ignored = make_item(item_type="reading", person_id="dev", person="Dev Shah", text="one more link", at=old)

    avoided = make_item(item_type="promise", person_id="tess", person="Tess", text="can you call the lawyer to decide")
    for _ in range(4):
        db.log_behavior("viewed", item_id=avoided.id)

    db.set_weight("pair:dev/reading", 0.5, observations=5)
    db.set_weight("pair:tess/promise", 0.5, observations=5)
    learning.apply_behavior_patterns(NOW)

    assert db.get_weight("pair:dev/reading") < 0.5, "deprioritized items should decay"
    assert db.get_weight("pair:tess/promise") == 0.5, "avoided items must never be decayed away"
    assert db.get_item(ignored.id).behavior_pattern == "deprioritized"


def test_weights_stay_inside_bounds():
    setup_people()
    item = make_item(person_id="dev", person="Dev Shah")
    for _ in range(200):
        learning.record("completed_manual", item)
    assert db.get_weight("person:dev") <= learning.MAX_WEIGHT
    for _ in range(200):
        learning.record("dismissed", item)
    assert db.get_weight("person:dev") >= learning.MIN_WEIGHT


def test_snapshot_groups_weights_by_kind():
    setup_people()
    learning.record("completed_manual", make_item(person_id="dev", person="Dev Shah"))
    snapshot = learning.snapshot()
    assert "dev" in snapshot["person"]
    assert snapshot["type"]
