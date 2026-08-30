"""jobs/poller.py — poll-cycle composition and idempotency. No live network."""
from __future__ import annotations

from datetime import timedelta

from conftest import NOW, make_message, make_person, make_conversation

from lifeline import db
from lifeline.ingestion import google_auth
from lifeline.jobs import poller


def setup_people():
    make_conversation()
    make_person("maya", "Maya", "spouse")


# ------------------------------------------------------------- poll_sources
def test_poll_sources_skips_cleanly_when_not_connected(monkeypatch):
    monkeypatch.setattr(poller.imessage, "poll", lambda: 0)
    assert google_auth.is_connected() is False
    result = poller.poll_sources()
    assert result == {"imessage": 0, "google": "not connected", "applecal": 0}


def test_poll_sources_calls_both_pollers_when_connected(monkeypatch):
    db.save_oauth_token("google", "AT", "RT", None,
                        ["https://www.googleapis.com/auth/calendar.readonly"])
    monkeypatch.setattr(poller.imessage, "poll", lambda: 0)
    monkeypatch.setattr(poller.gmail, "poll", lambda: 7)
    monkeypatch.setattr(poller.gcal, "poll", lambda: 3)

    result = poller.poll_sources()

    assert result == {"imessage": 0, "gmail": 7, "calendar": 3, "applecal": 0}


def test_poll_sources_isolates_a_gmail_failure_from_calendar():
    """One source erroring must not prevent the other from running or
    crash the whole poll cycle."""
    db.save_oauth_token("google", "AT", "RT", None,
                        ["https://www.googleapis.com/auth/calendar.readonly"])

    def explode():
        raise RuntimeError("gmail is down")

    import lifeline.jobs.poller as poller_module

    original_gcal_poll = poller_module.gcal.poll
    poller_module.gmail.poll = explode
    poller_module.gcal.poll = lambda: 4
    try:
        result = poller.poll_sources()
    finally:
        poller_module.gcal.poll = original_gcal_poll
        del poller_module.gmail.poll

    assert "gmail_error" in result and "gmail is down" in result["gmail_error"]
    assert result["calendar"] == 4


# -------------------------------------------------------------------- cycle
def test_cycle_runs_the_full_chain_and_returns_a_summary(monkeypatch):
    monkeypatch.setattr(poller.imessage, "poll", lambda: 0)
    setup_people()
    make_message("can you call the vet, it's urgent, by tomorrow")
    summary = poller.cycle(NOW)
    assert summary["sources"]["google"] == "not connected"
    assert summary["extracted"] == 1
    assert "completion" in summary and "learning" in summary and "notifications" in summary
    assert db.list_items()[0].score != 0.0, "items must be scored by the end of a cycle"


def test_cycle_is_idempotent_on_a_second_run():
    """A poll cycle with nothing new to ingest must not re-extract, re-close,
    or re-notify anything that a previous cycle already handled."""
    setup_people()
    make_message("can you call the vet, it's urgent, by tomorrow")
    first = poller.cycle(NOW)
    second = poller.cycle(NOW)

    assert first["extracted"] == 1
    assert second["extracted"] == 0
    assert len(db.list_items()) == 1
    assert second["notifications"]["time_sensitive"] == 0, "already-pushed items must not be re-queued"


def test_cycle_over_multiple_ingests_only_processes_whats_new():
    setup_people()
    make_message("can you call the vet, it's urgent, by tomorrow")
    poller.cycle(NOW)
    make_message("did you ever call the vet", at=NOW + timedelta(days=2))
    second = poller.cycle(NOW + timedelta(days=2))
    assert second["extracted"] == 1
    assert len(db.list_items()) == 2


def test_cycle_defaults_reference_to_now_when_omitted():
    setup_people()
    summary = poller.cycle()
    assert "at" in summary


def test_calendar_poll_skipped_without_scope(monkeypatch):
    db.save_oauth_token("google", "AT", "RT", None,
                        ["https://www.googleapis.com/auth/gmail.readonly"])  # no calendar
    monkeypatch.setattr(poller.imessage, "poll", lambda: 0)
    monkeypatch.setattr(poller.gmail, "poll", lambda: 1, raising=False)
    def boom(): raise AssertionError("calendar should not be polled")
    monkeypatch.setattr(poller.gcal, "poll", boom, raising=False)
    result = poller.poll_sources()
    assert result["calendar"] == "scope not granted"
