"""Notification model (§8.4)."""
from __future__ import annotations

from datetime import datetime, timedelta

from conftest import NOW, days_from_now, make_item, make_person, make_conversation

from lifeline import db
from lifeline.models import InterruptionLevel
from lifeline.notifications import apns, scheduler
from lifeline.ranking import scorer


def setup_people():
    make_conversation()
    make_person("maya", "Maya", "spouse")
    make_person("dev", "Dev Shah", "friend")


def morning(day_offset: int = 0) -> datetime:
    return (datetime(2026, 7, 27, 9, 0) + timedelta(days=day_offset)).astimezone()


def evening() -> datetime:
    return datetime(2026, 7, 27, 22, 0).astimezone()


def midday() -> datetime:
    return datetime(2026, 7, 27, 14, 0).astimezone()


# ----------------------------------------------------------- payload shape
def test_time_sensitive_payload_can_break_through_focus():
    payload = apns.build_payload("t", "b", InterruptionLevel.TIME_SENSITIVE)
    assert payload["aps"]["interruption-level"] == "time-sensitive"
    assert payload["aps"]["sound"] == "default"


def test_passive_payload_is_silent():
    payload = apns.build_payload("t", "b", InterruptionLevel.PASSIVE)
    assert payload["aps"]["interruption-level"] == "passive"
    assert "sound" not in payload["aps"]


def test_active_payload_shape():
    payload = apns.build_payload("t", "b", InterruptionLevel.ACTIVE, item_id="x1")
    assert payload["aps"]["interruption-level"] == "active"
    assert payload["item_id"] == "x1"


def test_send_dry_runs_without_apns_credentials():
    """Config in tests has no APNs key configured — send() must never try to
    sign a JWT or hit the network; it should just report a dry run."""
    payload = apns.build_payload("t", "b", InterruptionLevel.TIME_SENSITIVE)
    result = apns.send(["tok-1", "tok-2"], payload)
    assert result == {"tok-1": "dry-run", "tok-2": "dry-run"}


# ------------------------------------------------------------ time-sensitive
def test_time_sensitive_item_gets_queued_once():
    setup_people()
    item = make_item(date=days_from_now(1))
    scorer.score_item(item, NOW)
    db.save_item(item)
    assert item.interruption_level == InterruptionLevel.TIME_SENSITIVE

    first = scheduler.queue_time_sensitive(NOW)
    second = scheduler.queue_time_sensitive(NOW)
    assert len(first) == 1
    assert second == [], "an item already pushed must not be pushed again"

    rows = db.get_connection().execute(
        "SELECT * FROM notifications WHERE item_id = ? AND kind = 'item'", (item.id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["interruption"] == InterruptionLevel.TIME_SENSITIVE


def test_active_and_passive_items_never_get_a_solo_push():
    setup_people()
    active = make_item(text="no promises but eventually maybe", date=days_from_now(10))
    passive = make_item(text="no rush at all, whenever")
    scorer.rescore_all(NOW)
    assert db.get_item(active.id).interruption_level != InterruptionLevel.TIME_SENSITIVE
    assert db.get_item(passive.id).interruption_level == InterruptionLevel.PASSIVE
    assert scheduler.queue_time_sensitive(NOW) == []


def test_surfacing_a_push_logs_a_behavior_event():
    setup_people()
    item = make_item(date=days_from_now(1))
    scorer.score_item(item, NOW)
    db.save_item(item)
    scheduler.queue_time_sensitive(NOW)
    counts = db.behavior_counts(item.id)
    assert counts.get("surfaced") == 1


# ------------------------------------------------------------------ briefing
def test_briefing_only_fires_inside_the_morning_window():
    setup_people()
    make_item(date=days_from_now(1))
    scorer.rescore_all(NOW)
    assert scheduler.queue_morning_briefing(evening()) is None
    assert scheduler.queue_morning_briefing(midday()) is None
    assert scheduler.queue_morning_briefing(morning()) is not None


def test_briefing_fires_at_most_once_per_day():
    setup_people()
    make_item(date=days_from_now(1))
    scorer.rescore_all(NOW)
    first = scheduler.queue_morning_briefing(morning())
    second = scheduler.queue_morning_briefing(morning() + timedelta(hours=1))
    assert first is not None
    assert second is None


def test_briefing_fires_again_the_next_day():
    setup_people()
    make_item(date=days_from_now(3))
    scorer.rescore_all(NOW)
    assert scheduler.queue_morning_briefing(morning(0)) is not None
    assert scheduler.queue_morning_briefing(morning(1)) is not None


def test_briefing_requires_decision_items():
    """A queue of only Passive items shouldn't produce a briefing at all."""
    setup_people()
    make_item(text="no rush at all, whenever")
    scorer.rescore_all(NOW)
    assert scheduler.queue_morning_briefing(morning()) is None


def test_briefing_is_weighted_toward_decisions_not_passive():
    setup_people()
    make_item(text="no rush at all, whenever")
    make_item(item_type="promise", text="can you call the vet", date=days_from_now(1))
    scorer.rescore_all(NOW)
    notification_id = scheduler.queue_morning_briefing(morning())
    assert notification_id is not None
    row = db.get_connection().execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    assert "vet" not in row["title"].lower() or "thing" in row["title"].lower()
    assert row["interruption"] == InterruptionLevel.ACTIVE


def test_briefing_leads_with_the_active_wording_when_nothing_is_urgent():
    setup_people()
    make_item(item_type="promise", text="can you call the vet sometime", date=days_from_now(20))
    scorer.rescore_all(NOW)
    notification_id = scheduler.queue_morning_briefing(morning())
    row = db.get_connection().execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    assert "decide" in row["title"].lower()


# -------------------------------------------------------------------- digest
def test_passive_items_never_push_individually_only_batch():
    setup_people()
    make_item(text="no rush at all, whenever, thing one")
    make_item(text="no rush at all, whenever, thing two")
    scorer.rescore_all(NOW)
    assert scheduler.queue_time_sensitive(NOW) == []
    digest_id = scheduler.queue_passive_digest(NOW)
    assert digest_id is not None
    solo_pushes = db.get_connection().execute(
        "SELECT COUNT(*) AS n FROM notifications WHERE kind = 'item'"
    ).fetchone()["n"]
    assert solo_pushes == 0


def test_digest_requires_a_minimum_batch_size():
    setup_people()
    make_item(text="no rush at all, whenever, only one thing")
    scorer.rescore_all(NOW)
    assert scheduler.queue_passive_digest(NOW) is None


def test_digest_only_fires_once_per_interval():
    setup_people()
    make_item(text="no rush at all, whenever, thing one")
    make_item(text="no rush at all, whenever, thing two")
    scorer.rescore_all(NOW)
    first = scheduler.queue_passive_digest(NOW)
    second = scheduler.queue_passive_digest(NOW + timedelta(hours=1))
    assert first is not None
    assert second is None


def test_digest_fires_again_after_the_interval_with_new_items():
    setup_people()
    make_item(text="no rush at all, whenever, thing one")
    make_item(text="no rush at all, whenever, thing two")
    scorer.rescore_all(NOW)
    scheduler.queue_passive_digest(NOW)
    make_item(text="no rush at all, whenever, thing three", at=NOW + timedelta(hours=25))
    make_item(text="no rush at all, whenever, thing four", at=NOW + timedelta(hours=25))
    scorer.rescore_all(NOW + timedelta(hours=25))
    assert scheduler.queue_passive_digest(NOW + timedelta(hours=25)) is not None


def test_digest_never_repeats_an_already_digested_item():
    setup_people()
    make_item(text="no rush at all, whenever, thing one")
    make_item(text="no rush at all, whenever, thing two")
    scorer.rescore_all(NOW)
    scheduler.queue_passive_digest(NOW)
    make_item(text="no rush at all, whenever, thing three", at=NOW + timedelta(hours=25))
    scorer.rescore_all(NOW + timedelta(hours=25))
    # Only 1 fresh item now (thing three) — below the minimum batch size.
    assert scheduler.queue_passive_digest(NOW + timedelta(hours=25)) is None


def test_digest_names_the_senders():
    setup_people()
    make_item(text="no rush at all, whenever, thing one", person_id="maya", person="Maya")
    make_item(text="no rush at all, whenever, thing two", person_id="dev", person="Dev Shah")
    scorer.rescore_all(NOW)
    digest_id = scheduler.queue_passive_digest(NOW)
    row = db.get_connection().execute("SELECT * FROM notifications WHERE id = ?", (digest_id,)).fetchone()
    assert "Maya" in row["body"] and "Dev Shah" in row["body"]


# ---------------------------------------------------------------- completion
def test_completion_notification_is_celebratory_and_passive():
    setup_people()
    item = make_item(status="completed")
    notification_id = scheduler.queue_completion(item, evidence="matched a receipt")
    row = db.get_connection().execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    assert row["interruption"] == InterruptionLevel.PASSIVE
    assert row["body"] == "matched a receipt"


def test_completion_notification_is_not_duplicated():
    setup_people()
    item = make_item(status="completed")
    first = scheduler.queue_completion(item)
    second = scheduler.queue_completion(item)
    assert first is not None
    assert second is None


# ------------------------------------------------------------------ delivery
def test_flush_marks_everything_sent_and_never_double_sends():
    setup_people()
    item = make_item(date=days_from_now(1))
    scorer.score_item(item, NOW)
    db.save_item(item)
    scheduler.queue_time_sensitive(NOW)
    result = scheduler.flush(NOW)
    assert result["sent"] == 1
    assert not db.unsent_notifications()
    # A second flush has nothing left to do.
    assert scheduler.flush(NOW)["sent"] == 0


def test_flush_treats_per_item_digest_rows_as_bookkeeping_not_delivery():
    setup_people()
    make_item(text="no rush at all, whenever, thing one")
    make_item(text="no rush at all, whenever, thing two")
    scorer.rescore_all(NOW)
    scheduler.queue_passive_digest(NOW)
    result = scheduler.flush(NOW)
    # One real passive_digest delivery, two bookkeeping rows (one per item).
    assert result["sent"] == 1
    assert result["bookkeeping"] == 2


def test_flush_routes_completion_taps_to_the_claiming_thread(monkeypatch):
    """The dead-end tap: a completion row has an item but no finding, so its
    push carried no thread_id and tapping it went nowhere. The item's claiming
    thread is the destination."""
    from lifeline import threads
    from lifeline.models import Evidence

    setup_people()
    db.register_device("device-tok-1")
    item = make_item(status="completed")
    thread = threads.create(title="the loop this item served")
    db.add_evidence(Evidence(thread_id=thread.id, kind="item", ref_id=item.id))

    assert scheduler.queue_completion(item) is not None
    captured = {}
    monkeypatch.setattr(apns, "send",
                        lambda devices, payload, collapse_id=None:
                        captured.update(payload) or {})
    scheduler.flush(NOW)
    assert captured["thread_id"] == thread.id, "the tap has a door again"


def test_flush_reports_registered_device_count():
    setup_people()
    db.register_device("device-tok-1")
    db.register_device("device-tok-2")
    item = make_item(date=days_from_now(1))
    scorer.score_item(item, NOW)
    db.save_item(item)
    scheduler.queue_time_sensitive(NOW)
    result = scheduler.flush(NOW)
    assert result["devices"] == 2


# ------------------------------------------------------------------ run()
def test_run_is_a_safe_full_pass():
    setup_people()
    make_item(date=days_from_now(1))
    make_item(text="no rush at all, whenever, thing one")
    make_item(text="no rush at all, whenever, thing two")
    scorer.rescore_all(NOW)
    summary = scheduler.run(morning())
    assert summary["time_sensitive"] == 1
    assert summary["digest"] is True
    assert summary["delivery"]["sent"] >= 1
    # Nothing left dangling in the queue.
    assert not db.unsent_notifications()


def test_run_is_idempotent_across_repeated_calls():
    setup_people()
    make_item(date=days_from_now(1))
    scorer.rescore_all(NOW)
    scheduler.run(morning())
    second = scheduler.run(morning())
    assert second["time_sensitive"] == 0
