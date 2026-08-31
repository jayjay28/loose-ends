"""jobs/poller.py — poll-cycle composition and idempotency. No live network."""
from __future__ import annotations

from datetime import timedelta

from conftest import NOW, make_message, make_person, make_conversation

from lifeline import db
from lifeline.jobs import poller


def setup_people():
    make_conversation()
    make_person("tess", "Tess", "spouse")


# ------------------------------------------------------------- poll_sources
def test_poll_sources_reads_every_local_door(monkeypatch):
    """§v3: every source is local now — no account, no connection state, and
    nothing that can be "not configured" except an app the user doesn't use."""
    monkeypatch.setattr(poller.imessage, "poll", lambda: 0)
    monkeypatch.setattr(poller.applemail, "poll", lambda: 7)
    monkeypatch.setattr(poller.whatsapp, "poll", lambda: 2)
    monkeypatch.setattr(poller.notifications, "poll", lambda: 5)
    monkeypatch.setattr(poller.applecal, "poll", lambda: 3)

    result = poller.poll_sources()

    assert result == {"imessage": 0, "mail": 7, "whatsapp": 2,
                      "notifications": 5, "applecal": 3}


def test_poll_sources_isolates_one_failing_door(monkeypatch):
    """One source erroring must not stop the others or crash the cycle."""
    def explode():
        raise RuntimeError("mail is down")

    monkeypatch.setattr(poller.imessage, "poll", lambda: 0)
    monkeypatch.setattr(poller.applemail, "poll", explode)
    monkeypatch.setattr(poller.whatsapp, "poll", lambda: 0)
    monkeypatch.setattr(poller.notifications, "poll", lambda: 0)
    monkeypatch.setattr(poller.applecal, "poll", lambda: 4)

    result = poller.poll_sources()

    assert "mail_error" in result and "mail is down" in result["mail_error"]
    assert result["applecal"] == 4


def test_an_absent_mail_store_is_a_zero_not_a_failure(monkeypatch):
    """A Mac without Apple Mail set up is a normal Mac, not a broken one."""
    monkeypatch.setattr(poller.imessage, "poll", lambda: 0)
    monkeypatch.setattr(poller.applemail, "store_root", lambda: None)
    monkeypatch.setattr(poller.whatsapp, "poll", lambda: 0)
    monkeypatch.setattr(poller.notifications, "poll", lambda: 0)
    monkeypatch.setattr(poller.applecal, "poll", lambda: 0)

    result = poller.poll_sources()

    assert result["mail"] == 0 and "mail_error" not in result


# -------------------------------------------------------------------- cycle
def test_cycle_runs_the_full_chain_and_returns_a_summary(monkeypatch):
    monkeypatch.setattr(poller.imessage, "poll", lambda: 0)
    setup_people()
    make_message("can you call the vet, it's urgent, by tomorrow")
    summary = poller.cycle(NOW)
    assert summary["sources"]["imessage"] == 0
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
