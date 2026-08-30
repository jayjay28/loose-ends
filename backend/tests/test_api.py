"""Every route in api/app.py.

The TestClient below is deliberately used WITHOUT the `with` context manager:
entering it as a context manager triggers the app's lifespan, which starts
`poller.run_forever()` as a background asyncio task — a live poll loop with
no place in a unit test. Plain instantiation serves requests without running
startup/shutdown, and the per-test `fresh_db` fixture already migrates the
thread-local connection these routes need.
"""
from __future__ import annotations

from datetime import timedelta

from conftest import NOW, days_from_now, make_item, make_person, make_conversation
from fastapi.testclient import TestClient

from lifeline import db
from lifeline.api.app import app
from lifeline.completion import engine
from lifeline.models import CalendarEvent, InterruptionLevel
from lifeline.ranking import scorer

client = TestClient(app)


def setup_people():
    make_conversation()
    make_person("maya", "Maya", "spouse")
    make_person("dev", "Dev Shah", "friend")


# ----------------------------------------------------------------- health
def test_health_reports_configuration_state():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["google_connected"] is False
    assert body["claude_configured"] is False


# ------------------------------------------------------------------ today
def test_today_empty_state_is_a_distinct_mode():
    response = client.get("/today")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "empty"
    assert body["groups"] == []


def test_today_groups_by_interruption_level():
    setup_people()
    make_item(text="urgent thing", date=days_from_now(1))
    make_item(text="no rush at all, whenever, low key thing")
    scorer.rescore_all(NOW)
    response = client.get("/today", params={"at": NOW.isoformat()})
    body = response.json()
    levels = [g["level"] for g in body["groups"]]
    assert levels == sorted(levels, key=lambda l: InterruptionLevel.ORDER[l])
    assert body["counts"]["total"] == 2


def test_today_surge_mode_collapses_passive():
    setup_people()
    for n in range(5):
        make_item(text=f"urgent thing {n}", date=days_from_now(1))
    make_item(text="no rush at all, whenever")
    scorer.rescore_all(NOW)
    body = client.get("/today", params={"at": NOW.isoformat()}).json()
    assert body["mode"] == "surge"
    assert not any(g["level"] == InterruptionLevel.PASSIVE for g in body["groups"])


def test_today_includes_open_confirmations():
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
    engine.scan(NOW)
    body = client.get("/today", params={"at": NOW.isoformat()}).json()
    assert len(body["confirmations"]) == 1
    assert body["confirmations"][0]["item"]["id"] == item.id


def test_today_carries_the_why_explanation():
    """§8.3 transparency — the explanation must reach the client, not stay
    server-side trivia."""
    setup_people()
    make_item(text="urgent thing", date=days_from_now(1))
    scorer.rescore_all(NOW)
    body = client.get("/today", params={"at": NOW.isoformat()}).json()
    item = body["groups"][0]["items"][0]
    assert item["why"]
    assert {"signal", "detail", "contribution"} <= set(item["why"][0])


# ---------------------------------------------------------------- threads
def test_threads_lists_one_summary_per_person():
    setup_people()
    make_item(person_id="maya", person="Maya", text="thing one")
    make_item(person_id="maya", person="Maya", text="thing two")
    make_item(person_id="dev", person="Dev Shah", text="thing three")
    scorer.rescore_all(NOW)
    body = client.get("/conversations").json()
    by_person = {t["person"]: t for t in body}
    assert by_person["Maya"]["open_count"] == 2
    assert by_person["Dev Shah"]["open_count"] == 1


def test_thread_items_only_shows_that_persons_open_items():
    setup_people()
    make_item(person_id="maya", person="Maya", text="maya's thing")
    make_item(person_id="dev", person="Dev Shah", text="dev's thing")
    scorer.rescore_all(NOW)
    body = client.get("/conversations/maya").json()
    assert len(body) == 1
    assert body[0]["person"] == "Maya"


def test_thread_items_excludes_completed():
    setup_people()
    item = make_item(person_id="maya", person="Maya", text="maya's thing")
    engine.manual_close(item.id)
    assert client.get("/conversations/maya").json() == []


# ---------------------------------------------------------------- history
def test_history_reports_how_items_closed():
    setup_people()
    manual = make_item(person_id="maya", person="Maya", text="closed by hand")
    engine.manual_close(manual.id)

    auto = make_item(item_type="purchase", text="I want that Lemaire croissant bag", person_id="maya", person="Maya")
    auto.entities.item = "Lemaire croissant bag"
    db.save_item(auto)
    from lifeline.models import Message, Conversation, new_id

    db.upsert_conversation(Conversation(id="gmail:t1", source="gmail", display_name="shop"))
    db.insert_messages(
        [
            Message(
                id=new_id(),
                source="gmail",
                conversation_id="gmail:t1",
                external_id=new_id(),
                is_from_user=False,
                timestamp=(NOW + timedelta(days=1)).isoformat(timespec="seconds"),
                text="Your Lemaire order is confirmed\n\nCroissant Bag — $1,290.00",
                metadata={"subject": "Your Lemaire order is confirmed", "labels": ["CATEGORY_PURCHASES"]},
            )
        ]
    )
    engine.scan(NOW)

    body = client.get("/history").json()
    assert body["manual_closed"] == 1
    assert body["auto_closed"] == 1
    by_id = {e["item"]["id"]: e for e in body["entries"]}
    assert by_id[manual.id]["closed_by"] == "manual"
    assert by_id[auto.id]["closed_by"] == "auto"
    assert by_id[auto.id]["evidence"]


def test_history_excludes_open_items():
    setup_people()
    make_item(person_id="maya", person="Maya", text="still open")
    assert client.get("/history").json()["entries"] == []


# ------------------------------------------------------------------ items
def test_item_detail_returns_full_payload():
    setup_people()
    item = make_item(person_id="maya", person="Maya", text="a thing")
    body = client.get(f"/items/{item.id}").json()
    assert body["id"] == item.id
    assert body["person"] == "Maya"


def test_item_detail_404_for_unknown_id():
    assert client.get("/items/does-not-exist").status_code == 404


# ---------------------------------------------------------------- actions
def test_view_logs_a_viewed_behavior_event():
    setup_people()
    item = make_item(person_id="maya", person="Maya")
    response = client.post(f"/items/{item.id}/view")
    assert response.status_code == 200
    assert db.behavior_counts(item.id).get("viewed") == 1


def test_view_expanded_logs_an_expanded_event():
    setup_people()
    item = make_item(person_id="maya", person="Maya")
    client.post(f"/items/{item.id}/view", params={"expanded": True})
    assert db.behavior_counts(item.id).get("expanded") == 1


def test_act_records_acted_and_reinforces_the_sender_weight():
    setup_people()
    item = make_item(person_id="dev", person="Dev Shah")
    before = db.get_weight("person:dev", 0.6)
    response = client.post(f"/items/{item.id}/act")
    assert response.status_code == 200
    assert db.behavior_counts(item.id).get("acted") == 1
    assert db.get_weight("person:dev", 0.6) > before


def test_done_closes_the_item_manually():
    setup_people()
    item = make_item(person_id="maya", person="Maya")
    response = client.post(f"/items/{item.id}/done")
    assert response.status_code == 200
    assert response.json()["item"]["status"] == "completed"
    assert db.get_item(item.id).completed_by == "manual"


def test_done_404_for_unknown_item():
    assert client.post("/items/nope/done").status_code == 404


def test_snooze_with_explicit_hours():
    setup_people()
    item = make_item(person_id="maya", person="Maya")
    response = client.post(f"/items/{item.id}/snooze", json={"hours": 2})
    assert response.status_code == 200
    stored = db.get_item(item.id)
    assert stored.status == "snoozed"
    assert stored.snoozed_until is not None
    assert db.behavior_counts(item.id).get("snoozed") == 1


def test_snooze_with_explicit_until():
    setup_people()
    item = make_item(person_id="maya", person="Maya")
    until = "2026-08-01T09:00:00+00:00"
    response = client.post(f"/items/{item.id}/snooze", json={"until": until})
    assert response.json()["item"]["snoozed_until"] == until


def test_snooze_defaults_to_24_hours():
    setup_people()
    item = make_item(person_id="maya", person="Maya")
    client.post(f"/items/{item.id}/snooze", json={})
    stored = db.get_item(item.id)
    assert stored.snoozed_until is not None


def test_dismiss_marks_dismissed_and_feeds_learning():
    setup_people()
    item = make_item(item_type="reading", person_id="dev", person="Dev Shah")
    response = client.post(f"/items/{item.id}/dismiss")
    assert response.status_code == 200
    assert db.get_item(item.id).status == "dismissed"
    assert db.get_weight("pair:dev/reading", 0.5) < 0.5


# ---------------------------------------------------------- confirmations
def _make_fuzzy_confirmation():
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
    return item, outcome.needs_confirmation[0]


def test_confirmations_lists_open_fuzzy_matches():
    item, signal = _make_fuzzy_confirmation()
    body = client.get("/confirmations").json()
    assert len(body) == 1
    assert body[0]["signal_id"] == signal.id
    assert body[0]["item"]["id"] == item.id


def test_confirmations_accept_closes_the_item():
    item, signal = _make_fuzzy_confirmation()
    response = client.post(f"/confirmations/{signal.id}/confirm")
    assert response.status_code == 200
    assert response.json()["item"]["status"] == "completed"
    assert client.get("/confirmations").json() == []


def test_confirmations_reject_keeps_the_item_open():
    item, signal = _make_fuzzy_confirmation()
    response = client.post(f"/confirmations/{signal.id}/reject")
    assert response.status_code == 200
    assert response.json()["item"]["status"] == "pending"
    assert client.get("/confirmations").json() == []


def test_confirm_404_for_unknown_signal():
    assert client.post("/confirmations/nope/confirm").status_code == 404


def test_reject_404_for_unknown_signal():
    assert client.post("/confirmations/nope/reject").status_code == 404


# ------------------------------------------------------------------- sync
def test_sync_changes_returns_everything_with_no_since():
    setup_people()
    make_item(person_id="maya", person="Maya")
    make_item(person_id="dev", person="Dev Shah")
    body = client.get("/sync/changes").json()
    assert len(body["items"]) == 2
    assert "server_time" in body


def test_sync_changes_since_is_incremental():
    """`updated_at` is second-precision real wall-clock time (db.save_item
    always stamps `now_iso()`), so a fast in-process test can land both
    writes in the same second as the checkpoint. Push the second item's
    stamp forward explicitly rather than depending on wall-clock timing.
    """
    setup_people()
    make_item(person_id="maya", person="Maya")
    checkpoint = client.get("/sync/changes").json()["server_time"]
    later = make_item(person_id="dev", person="Dev Shah")
    # `updated_at` is real wall-clock time (db.save_item always stamps
    # now_iso()), unrelated to the fixture's fictional NOW — push forward
    # from the real checkpoint, not from NOW.
    from lifeline.models import parse_iso

    future = (parse_iso(checkpoint) + timedelta(seconds=5)).isoformat(timespec="seconds")
    db.get_connection().execute("UPDATE items SET updated_at = ? WHERE id = ?", (future, later.id))
    db.get_connection().commit()

    delta = client.get("/sync/changes", params={"since": checkpoint}).json()
    assert len(delta["items"]) == 1
    assert delta["items"][0]["person"] == "Dev Shah"


def test_sync_changes_since_now_is_empty():
    setup_people()
    make_item(person_id="maya", person="Maya")
    now = client.get("/sync/changes").json()["server_time"]
    assert client.get("/sync/changes", params={"since": now}).json()["items"] == []


def test_sync_changes_includes_open_confirmations():
    item, signal = _make_fuzzy_confirmation()
    body = client.get("/sync/changes").json()
    assert len(body["confirmations"]) == 1
    assert body["confirmations"][0]["signal_id"] == signal.id


def test_sync_poll_waits_and_returns_a_summary():
    setup_people()
    make_item(person_id="maya", person="Maya", text="can you call the vet by tomorrow")
    response = client.post("/sync/poll", params={"wait": True})
    assert response.status_code == 200
    body = response.json()
    assert "completion" in body and "notifications" in body


def test_sync_poll_without_wait_starts_in_background():
    response = client.post("/sync/poll")
    assert response.status_code == 200
    assert response.json() == {"started": True}


# ------------------------------------------------------------------ model
def test_model_snapshot_reports_weights_and_patterns():
    setup_people()
    item = make_item(person_id="dev", person="Dev Shah")
    from lifeline.ranking import learning

    learning.record("completed_manual", item)
    body = client.get("/model/weights").json()
    assert "dev" in body["weights"]["person"]
    assert "static_signal_weights" in body
    assert "avoidance" in body["patterns"] and "deprioritized" in body["patterns"]


# ---------------------------------------------------------------- devices
def test_register_device_increments_count():
    response = client.post("/devices", json={"token": "abc123"})
    assert response.status_code == 200
    assert response.json()["devices"] >= 1
    assert "abc123" in db.list_devices()


# -------------------------------------------------------------- ingestion
def test_ingest_import_rejects_missing_file():
    response = client.post("/ingest/import", json={"source": "imessage", "path": "/no/such/file.json"})
    assert response.status_code == 400


def test_ingest_import_rejects_unknown_source(tmp_path):
    fixture = tmp_path / "export.json"
    fixture.write_text("{}")
    response = client.post("/ingest/import", json={"source": "carrier-pigeon", "path": str(fixture)})
    assert response.status_code == 400


def test_ingest_import_imessage_export(sample_dir):
    response = client.post(
        "/ingest/import", json={"source": "imessage", "path": str(sample_dir / "imessage_export.json")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["messages_imported"] > 0
    assert body["items_extracted"] > 0


# ---------------------------------------------------------------- google
def test_google_start_fails_cleanly_without_credentials():
    """No client id/secret configured in the test environment — must not
    attempt any network call, just report the missing configuration."""
    response = client.get("/auth/google/start", follow_redirects=False)
    assert response.status_code == 400


def test_google_callback_reports_provider_error():
    response = client.get("/auth/google/callback", params={"error": "access_denied"})
    assert response.status_code == 400
    assert "access_denied" in response.text


def test_google_callback_requires_a_code():
    response = client.get("/auth/google/callback")
    assert response.status_code == 400


def test_google_disconnect_is_always_safe():
    response = client.post("/auth/google/disconnect")
    assert response.status_code == 200
    assert response.json() == {"connected": False}


# ------------------------------------------------------------------ admin
def test_purge_clears_all_extracted_data():
    setup_people()
    make_item(person_id="maya", person="Maya")
    assert client.post("/admin/purge").json() == {"purged": True}
    assert db.list_items() == []


def test_load_sample_runs_the_whole_pipeline(sample_dir):
    body = client.post("/admin/load-sample").json()
    assert body["ingested"]["imessage"] == 12
    assert body["extracted"] > 0
    assert "auto_closed" in body["completion"]


# ---------------------------------------------------------------- briefing
def test_briefing_empty_state_is_caught_up():
    setup_people()
    body = client.get("/briefing", params={"at": NOW.isoformat()}).json()
    assert body["caught_up"] is True
    assert body["one_now"] is None
    assert body["waiting"] == []


def test_briefing_surfaces_one_now_and_ranks_waiting_by_tie():
    setup_people()
    make_item(person_id="maya", person="Maya", item_type="question", text="can you confirm dinner")
    make_item(person_id="dev", person="Dev Shah", item_type="promise", text="send the deck over")
    make_item(person_id="dev", person="Dev Shah", item_type="reading", text="read this later")
    # Both the scoring and the read are pinned to the fixture clock.
    # `rescore_all()` is where interruption_level is decided, so leaving it on
    # the wall clock decays these items to PASSIVE as real time drifts from
    # NOW — and then `actionable` filters them all out no matter what `at` the
    # briefing is given.
    scorer.rescore_all(NOW)
    # Pinned to the fixture clock. `/briefing` ranks against real `now` when no
    # `at` is given, and these items are created at NOW — so as wall-clock time
    # drifts away from the fixture they decay to PASSIVE, drop out of
    # `actionable`, and `waiting` silently empties. The test then fails on a
    # date rather than on a defect.
    body = client.get("/briefing", params={"at": NOW.isoformat()}).json()
    assert body["caught_up"] is False
    # one_now must be the decision, not the reading link
    assert body["one_now"] is not None
    assert body["one_now"]["type"] != "reading"
    # waiting is sorted by tie strength (desc)
    ties = [w["tie_strength"] for w in body["waiting"]]
    assert ties == sorted(ties, reverse=True)
    # reading isn't "someone waiting on you" — it's excluded, and doesn't inflate counts
    assert {w["person"] for w in body["waiting"]} == {"Maya", "Dev Shah"}
    dev = next(w for w in body["waiting"] if w["person"] == "Dev Shah")
    assert dev["open_count"] == 1


# -------------------------------------------------------------------- /ask
def test_ask_reports_what_you_owe_a_person():
    setup_people()
    make_item(person_id="maya", person="Maya", item_type="promise", text="send the invoice over")
    body = client.post("/ask", json={"question": "what do I owe Maya?"}).json()
    assert "Maya" in body["answer"]
    assert body["sources"]


def test_ask_rejects_empty_question():
    assert client.post("/ask", json={"question": "   "}).status_code == 400


# ------------------------------------------------------------- calendar sync
def test_calendar_sync_stores_device_events():
    setup_people()
    body = {"events": [{"id": "e1", "summary": "Milo school trip", "start_at": days_from_now(1)}]}
    r = client.post("/calendar/events", json=body)
    assert r.status_code == 200
    assert r.json()["stored"] == 1
    assert len(db.list_calendar_events()) == 1


# --------------------------------------------------------------- enrichment
def test_item_enriched_returns_grounded_headline():
    setup_people()
    it = make_item(person_id="maya", person="Maya", item_type="question", text="can you confirm dinner")
    body = client.get(f"/items/{it.id}/enriched").json()
    assert body["headline"]
    assert "briefing" in body
    assert isinstance(body["sources"], list)


def test_similar_message_stats_detects_a_recurring_pattern():
    from lifeline.assistant import tools
    from lifeline.models import Message, new_id
    make_conversation(); make_person("maya", "Maya")
    for n in range(3):
        db.insert_messages([Message(
            id=new_id(), source="imessage", conversation_id="imessage:t1", external_id=new_id(),
            person_id="maya", is_from_user=False,
            timestamp=days_from_now(-n * 7), text="your parking permit renewal is due")])
    it = make_item(person_id="maya", person="Maya", text="your parking permit renewal is due")
    stats = tools.similar_message_stats(it)
    assert stats["count"] >= 3 and stats["recurring"] is True


# --------------------------------------------------------------- dossier
def test_dossier_surfaces_why_and_your_last_word():
    from conftest import make_message
    setup_people(); make_conversation()
    make_message("can you send the deck?", is_from_user=False, at=NOW - timedelta(minutes=5))
    make_message("on it, tomorrow", is_from_user=True, at=NOW)   # you spoke last
    it = make_item(person_id="maya", person="Maya", item_type="question", text="can you send the deck?")
    scorer.rescore_all(NOW)
    d = client.get(f"/items/{it.id}/dossier").json()
    assert d["your_last_word"]["text"] == "on it, tomorrow"
    assert d["awaiting_reply"] is True          # you spoke last, no answer back
    assert isinstance(d["why"], list)
    assert all("learned weight" not in w for w in d["why"])   # jargon filtered
