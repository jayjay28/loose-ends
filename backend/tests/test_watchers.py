"""§v2 step 6 — watchers: standing monitors a thread implies.

"Is the flight delayed?" is not a question the user asked. The tests here
protect three properties that make a monitor affordable enough to run
constantly: it does no thinking, it feeds the worker rather than duplicating
it, and it retires itself when its reason is gone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from lifeline import db, threads
from lifeline.api.app import app
from lifeline.assistant import registry, worker
from lifeline.models import CalendarEvent, DeadlineSource, Message, ThreadState, new_id
from lifeline.threads import watchers
from lifeline.threads.watchers import WatchKind

from tests.conftest import NOW, days_from_now, make_conversation, make_message, make_person


@pytest.fixture(autouse=True)
def default_person():
    make_person()
    make_conversation()


@pytest.fixture
def client():
    return TestClient(app)


def _mail(subject, from_email, at, text="body"):
    make_conversation("gmail:t1", source="gmail", name="Mail")
    message = Message(
        id=new_id(), source="gmail", conversation_id="gmail:t1", external_id=new_id(),
        person_id=None, is_from_user=False, timestamp=at.isoformat(timespec="seconds"),
        text=f"{subject}\n{text}",
        metadata={"subject": subject, "from_email": from_email, "labels": ["INBOX"]},
    )
    db.insert_messages([message])
    return message


# ------------------------------------------------------------- setting one
def test_a_watcher_belongs_to_a_thread_and_reads_plainly():
    thread = threads.create(title="Puerto Rico work trip")
    w = watchers.add(thread.id, WatchKind.MAIL, "anything from the airline",
                     spec={"sender": "united.com"}, cadence_minutes=180)
    assert w.thread_id == thread.id
    assert [x.id for x in watchers.for_thread(thread.id)] == [w.id]


def test_setting_the_same_watch_twice_is_the_same_watcher():
    """The worker re-derives a thread's monitors every pass, so without this it
    would stack up duplicates of the same watch."""
    thread = threads.create(title="a trip")
    a = watchers.add(thread.id, WatchKind.MAIL, "airline mail", spec={"sender": "united.com"})
    b = watchers.add(thread.id, WatchKind.MAIL, "airline mail again", spec={"sender": "united.com"})
    assert a.id == b.id
    assert len(watchers.for_thread(thread.id)) == 1


def test_cadence_has_a_floor():
    """The poller ticks every five minutes and nothing local moves faster in a
    way worth waking for."""
    thread = threads.create(title="a trip")
    w = watchers.add(thread.id, WatchKind.MAIL, "x", cadence_minutes=1)
    assert w.cadence_minutes == watchers.MIN_CADENCE_MINUTES


def test_an_unknown_kind_is_refused():
    thread = threads.create(title="a trip")
    with pytest.raises(threads.ThreadError):
        watchers.add(thread.id, "telepathy", "what the user is thinking")


# ------------------------------------------------------------- firing
def test_a_mail_watcher_attaches_what_it_finds():
    """The whole mechanism: a watcher doesn't interpret, it attaches evidence —
    and that is exactly what makes the thread due for the worker."""
    thread = threads.create(title="Pay the water bill")
    w = watchers.add(thread.id, WatchKind.MAIL, "mail from American Water",
                     spec={"sender": "amwater.com"})
    w.last_checked_at = (NOW - timedelta(hours=6)).isoformat(timespec="seconds")
    db.save_watcher(w)

    message = _mail("Your bill is ready", "billing@cs.amwater.com", NOW - timedelta(hours=1))
    found = watchers.check(db.get_watcher(w.id), NOW)

    assert [f["ref_id"] for f in found] == [message.id]
    assert message.id in {e.ref_id for e in db.thread_evidence(thread.id)}


def test_firing_makes_the_thread_due_for_the_worker():
    """Watchers and the worker are one pipeline: the watcher notices, the
    worker explains. Nothing else connects them."""
    thread = threads.create(title="Pay the water bill")
    thread.last_worked_at = NOW.isoformat(timespec="seconds")
    db.save_thread(thread)
    assert worker.due(NOW + timedelta(minutes=1)) == []

    w = watchers.add(thread.id, WatchKind.MAIL, "bills", spec={"sender": "amwater.com"})
    w.last_checked_at = (NOW - timedelta(hours=6)).isoformat(timespec="seconds")
    db.save_watcher(w)
    _mail("Your bill is ready", "billing@cs.amwater.com", NOW + timedelta(minutes=2))
    watchers.check(db.get_watcher(w.id), NOW + timedelta(minutes=3))

    assert [t.id for t in worker.due(NOW + timedelta(minutes=4))] == [thread.id]


def test_a_calendar_watcher_notices_an_event_that_moved():
    """A flight time changing is the same event with a new time — matching on
    the id alone would never see it. `updated_at` is what makes change visible."""
    thread = threads.create(title="Puerto Rico work trip")
    w = watchers.add(thread.id, WatchKind.CALENDAR, "the flight", spec={"query": "flight"})
    w.last_checked_at = (NOW - timedelta(hours=6)).isoformat(timespec="seconds")
    db.save_watcher(w)

    db.upsert_calendar_events([CalendarEvent(
        id="ua1287", calendar_id="c", summary="Flight UA1287 to SJU",
        start_at=days_from_now(2, base=NOW),
        updated_at=(NOW - timedelta(minutes=10)).isoformat(timespec="seconds"),
    )])
    found = watchers.check(db.get_watcher(w.id), NOW)
    assert [f["ref_id"] for f in found] == ["ua1287"]


def test_a_deadline_watcher_fires_on_time_passing_with_no_new_data():
    """The one that notices when *nothing arrives*. Without it, a thread with a
    date and a quiet inbox is never revisited and the system only ever reacts
    to other people."""
    thread = threads.create(title="Sign kid up for soccer")
    threads.set_deadline(thread.id, days_from_now(2, base=NOW), source=DeadlineSource.USER)
    thread = db.get_thread(thread.id)
    thread.last_worked_at = NOW.isoformat(timespec="seconds")
    db.save_thread(thread)

    w = watchers.add(thread.id, WatchKind.DEADLINE, "the registration date",
                     spec={"days_before": 3})
    watchers.check(db.get_watcher(w.id), NOW)

    # It attaches nothing — the thread itself is the news — but it does make
    # the thread due again.
    assert db.get_thread(thread.id).last_worked_at is None
    assert [t.id for t in worker.due(NOW)] == [thread.id]


def test_a_watcher_does_not_refire_on_what_it_already_saw():
    thread = threads.create(title="Pay the water bill")
    w = watchers.add(thread.id, WatchKind.MAIL, "bills", spec={"sender": "amwater.com"})
    w.last_checked_at = (NOW - timedelta(hours=6)).isoformat(timespec="seconds")
    db.save_watcher(w)
    _mail("Your bill is ready", "billing@cs.amwater.com", NOW - timedelta(hours=1))

    assert watchers.check(db.get_watcher(w.id), NOW)
    assert watchers.check(db.get_watcher(w.id), NOW + timedelta(hours=1)) == []


# -------------------------------------------------------------- the sweep
def test_the_sweep_respects_cadence():
    thread = threads.create(title="a trip")
    watchers.add(thread.id, WatchKind.MAIL, "x", spec={"sender": "a.com"}, cadence_minutes=180)

    assert watchers.sweep(NOW)["checked"] == 1
    assert watchers.sweep(NOW + timedelta(minutes=30))["checked"] == 0
    assert watchers.sweep(NOW + timedelta(hours=4))["checked"] == 1


def test_a_watcher_retires_when_its_thread_closes():
    """A monitor that outlives its reason is a scheduled way to waste money."""
    thread = threads.create(title="a trip")
    watchers.add(thread.id, WatchKind.MAIL, "x", spec={"sender": "a.com"})
    threads.resolve(thread.id)

    assert watchers.sweep(NOW)["expired"] == 1
    assert watchers.for_thread(thread.id) == []
    assert watchers.for_thread(thread.id, include_expired=True)


def test_a_watcher_retires_when_its_until_passes():
    """"Every 3h until departure" has an end."""
    thread = threads.create(title="a trip")
    watchers.add(thread.id, WatchKind.MAIL, "x", spec={"sender": "a.com"},
                 until=days_from_now(-1, base=NOW))
    assert watchers.sweep(NOW)["expired"] == 1


def test_the_poller_sweeps_watchers_before_the_worker():
    """Order matters: a watcher that fires this cycle attaches evidence, which
    is what makes the thread due for the worker on the *same* cycle."""
    from lifeline.jobs import poller

    thread = threads.create(title="a trip")
    watchers.add(thread.id, WatchKind.MAIL, "x", spec={"sender": "a.com"})
    summary = poller.cycle(NOW)
    assert summary["watchers"]["checked"] == 1


def test_the_sweep_costs_no_model_calls(monkeypatch):
    """A watcher that needed a model per check would cost one call per cadence
    tick per thread — the arithmetic that made the worker's cadence a decision."""
    from lifeline.extraction import providers

    def explode():
        raise AssertionError("the watcher sweep must not reach a provider")

    monkeypatch.setattr(providers, "available", explode)
    thread = threads.create(title="a trip")
    watchers.add(thread.id, WatchKind.MAIL, "x", spec={"sender": "a.com"})
    watchers.sweep(NOW)


# --------------------------------------------------------------- the tools
def test_the_worker_can_set_and_list_watchers():
    thread = threads.create(title="Puerto Rico work trip")
    by_name = registry.by_name(registry.scoped_for(thread))
    assert {"add_watcher", "list_watchers", "stop_watching"} <= set(by_name)

    by_name["add_watcher"].fn(
        what="mail from the airline", kind="mail", sender="united.com", every_hours=3
    )
    listed = by_name["list_watchers"].fn()
    assert listed[0]["what"] == "mail from the airline"
    assert listed[0]["every_minutes"] == 180


def test_a_watcher_tool_is_bound_to_its_thread():
    a = threads.create(title="thread A")
    b = threads.create(title="thread B")
    registry.by_name(registry.scoped_for(a))["add_watcher"].fn(what="only A", sender="a.com")
    assert len(watchers.for_thread(a.id)) == 1
    assert watchers.for_thread(b.id) == []


def test_read_thread_state_shows_what_is_being_watched():
    """The worker must see its own monitors or it re-derives them every pass."""
    thread = threads.create(title="a trip")
    watchers.add(thread.id, WatchKind.MAIL, "airline mail", spec={"sender": "united.com"})
    state = threads.read_state(thread.id)
    assert state["watchers"][0]["what"] == "airline mail"


# ---------------------------------------------------------------- the wire
def test_watchers_reach_the_thread_detail(client):
    thread = threads.create(title="a trip")
    watchers.add(thread.id, WatchKind.MAIL, "airline mail", spec={"sender": "united.com"})
    body = client.get(f"/threads/{thread.id}").json()
    assert body["watchers"][0]["what"] == "airline mail"
    assert body["watchers"][0]["times_fired"] == 0


def test_the_user_can_switch_a_watcher_off(client):
    """An autonomous watch nobody can switch off is surveillance."""
    thread = threads.create(title="a trip")
    w = watchers.add(thread.id, WatchKind.MAIL, "airline mail", spec={"sender": "united.com"})
    body = client.delete(f"/threads/{thread.id}/watchers/{w.id}").json()
    assert body["watchers"] == []
    assert client.delete(f"/threads/{thread.id}/watchers/{w.id}").status_code == 404
