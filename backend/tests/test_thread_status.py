"""One word for what the system has done with a thread.

A thread added a minute ago and a thread the worker checked an hour ago and
found nothing used to render identically. Silence meant both "nothing to
report" and "nobody has looked", and the app had no way to tell them apart.
"""
from __future__ import annotations

from datetime import timedelta

from conftest import NOW, make_person
from lifeline import db, threads as threads_mod
from lifeline.models import DeadlineSource, Finding, Thread, ThreadState


def _live(**kw) -> Thread:
    return threads_mod.create(title=kw.pop("title", "a thread"), **kw)


def _worked(thread: Thread) -> Thread:
    """What `worker.work` does at the end of a pass (worker.py:356)."""
    thread.last_worked_at = NOW.isoformat(timespec="seconds")
    db.save_thread(thread)
    return db.get_thread(thread.id)


def test_a_thread_nobody_has_worked_is_queued():
    thread = _live(title="I need to start my blog")
    assert thread.last_worked_at is None
    assert threads_mod.status(thread, NOW) == threads_mod.Status.QUEUED


def test_a_worked_thread_is_ordinary():
    """The common case must stay unlabelled, or the labels are wallpaper."""
    assert threads_mod.status(_worked(_live()), NOW) == threads_mod.Status.NONE


def test_a_passed_date_outranks_never_having_been_looked_at():
    thread = _live()
    threads_mod.set_deadline(
        thread.id, (NOW - timedelta(days=3)).isoformat(), source=DeadlineSource.USER
    )
    # still never worked, but the date is the thing the reader needs first
    assert threads_mod.status(db.get_thread(thread.id), NOW) == threads_mod.Status.OVERDUE


def test_a_future_date_is_not_overdue():
    thread = _live()
    threads_mod.set_deadline(
        thread.id, (NOW + timedelta(days=3)).isoformat(), source=DeadlineSource.USER
    )
    _worked(thread)
    assert threads_mod.status(db.get_thread(thread.id), NOW) == threads_mod.Status.NONE


def test_a_blocked_move_needs_you():
    thread = _live()
    _worked(thread)
    db.save_finding(Finding(
        thread_id=thread.id, kind="action", headline="pick a size",
        blocked_reason="her size — M unless you say otherwise",
    ))
    assert threads_mod.status(db.get_thread(thread.id), NOW) == threads_mod.Status.NEEDS_YOU


def test_an_unblocked_move_is_not_a_question():
    thread = _live()
    _worked(thread)
    db.save_finding(Finding(thread_id=thread.id, kind="action", headline="buy the hoop"))
    assert threads_mod.status(db.get_thread(thread.id), NOW) == threads_mod.Status.NONE


def test_a_closed_thread_looks_finished():
    thread = _live()
    threads_mod.resolve(thread.id)
    assert threads_mod.status(db.get_thread(thread.id), NOW) == threads_mod.Status.FINISHED


def test_watching_is_not_a_status():
    """It labelled nine of eleven live threads — the normal condition of a
    thread, not news. A status that fits everything signals nothing."""
    assert "watching" not in threads_mod.Status.LABEL
    assert not hasattr(threads_mod.Status, "WATCHING")


def test_every_status_except_none_has_wording():
    codes = {
        v for k, v in vars(threads_mod.Status).items()
        if k.isupper() and isinstance(v, str) and v != threads_mod.Status.NONE
    }
    assert codes == set(threads_mod.Status.LABEL)


def test_the_status_reaches_the_wire():
    from fastapi.testclient import TestClient
    from lifeline.api.app import app

    make_person()
    _live(title="never touched")
    body = TestClient(app).get("/threads").json()
    rows = body["threads"] if isinstance(body, dict) else body
    queued = [t for t in rows if t["title"] == "never touched"]
    assert queued, "the thread should be on the stack"
    assert queued[0]["status"] == "queued"
    assert queued[0]["status_label"] == "Queued"
