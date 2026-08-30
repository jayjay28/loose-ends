"""§v2 step 5 — evidence-based closure: the mechanism that shrinks the pile.

Everything before this step only adds to it. The tests are grouped by the two
promises that make closing-on-someone's-behalf acceptable: it only happens on
definite evidence, and it can always say why.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from lifeline import db, threads
from lifeline.api.app import app
from lifeline.completion import engine
from lifeline.models import DeadlineSource, Evidence, Message, ThreadState, new_id
from lifeline.threads import closure

from tests.conftest import NOW, days_from_now, make_conversation, make_item, make_message, make_person


@pytest.fixture(autouse=True)
def default_person():
    make_person()
    make_conversation()


@pytest.fixture
def client():
    return TestClient(app)


def _thread_with_items(*items, title="a loop"):
    return threads.create(
        title=title,
        evidence=[Evidence(kind="item", ref_id=i.id) for i in items],
    )


# ---------------------------------------------- the evidence that closes it
def test_a_thread_closes_when_everything_it_tracked_is_done():
    """A thread's strongest signal is its own evidence closing. Threads claim
    items, and v1.5 already closes items well — this reuses machinery that has
    earned its confidence rather than inventing a new judgment."""
    item = make_item(text="pay the water bill")
    thread = _thread_with_items(item, title="Pay the water bill")

    assert closure.match(thread, NOW).resolution is None    # nothing closed yet

    engine.manual_close(item.id)
    result = closure.match(db.get_thread(thread.id), NOW)
    assert result.resolution == "auto_closed"
    assert result.definite is True
    assert any("done" in r for r in result.reasons)


def test_partly_done_is_not_done():
    a = make_item(text="first half")
    b = make_item(text="second half")
    thread = _thread_with_items(a, b)
    engine.manual_close(a.id)

    result = closure.match(db.get_thread(thread.id), NOW)
    assert result.resolution is None
    assert "1 of 2" in " ".join(result.reasons)


def test_a_passed_date_asks_rather_than_closes():
    """Not proof: the flight may have been missed, the bill may be overdue
    rather than paid. The date going by is a question, not a decision."""
    thread = threads.create(title="Skating with the kids")
    threads.set_deadline(thread.id, days_from_now(-30, base=NOW), source=DeadlineSource.USER)

    result = closure.match(db.get_thread(thread.id), NOW)
    assert result.resolution == "needs_confirmation"
    assert "passed" in " ".join(result.reasons)


def test_your_own_reply_counts_when_it_is_about_the_thread():
    """v1.5's single most productive closer — 76 of its auto-closes came from
    the user replying in the same conversation."""
    item = make_item(text="can you send the plumber's number")
    thread = _thread_with_items(item, title="Plumber for the Detroit house")
    make_message("got the plumber booked, thanks", is_from_user=True,
                 at=datetime.fromisoformat(thread.opened_at) + timedelta(hours=1))

    result = closure.match(db.get_thread(thread.id), NOW)
    assert any("you replied" in r for r in result.reasons)


def test_a_reply_about_something_else_is_not_evidence():
    """People text the same person about eleven other things. The old rule read
    a conversation as if it had one subject: the thread opened, the user texted
    that person, therefore the thread is done. Live, this made "😒" and "Where
    are you" the case for closing a thread about buying a basketball hoop."""
    item = make_item(text="can you send the plumber's number")
    thread = _thread_with_items(item, title="Plumber for the Detroit house")
    make_message("Where are you", is_from_user=True,
                 at=datetime.fromisoformat(thread.opened_at) + timedelta(hours=1))

    result = closure.match(db.get_thread(thread.id), NOW)
    assert not any("you replied" in r for r in result.reasons)
    assert result.resolution is None


def test_a_receipt_alone_never_closes_anything():
    """The shape of signal that produced 37 rejections and 2 confirmations for
    items. It may contribute; it may never be the whole case."""
    thread = threads.create(title="soccer registration")
    make_message(
        "Registration complete — your soccer registration is confirmed",
        is_from_user=False,
        at=datetime.fromisoformat(thread.opened_at) + timedelta(hours=1),
    )
    result = closure.match(db.get_thread(thread.id), NOW)
    assert result.resolution != "auto_closed"
    assert result.definite is False


def test_a_fuzzy_part_stops_an_otherwise_definite_case_from_auto_closing():
    """The never-auto-close-a-fuzzy-match rule, carried over from §7. One
    inexact part makes the whole thing a question."""
    item = make_item(text="soccer registration")
    thread = _thread_with_items(item, title="soccer registration")
    engine.manual_close(item.id)
    make_message(
        "Registration complete — soccer registration confirmed", is_from_user=False,
        at=datetime.fromisoformat(thread.opened_at) + timedelta(hours=1),
    )
    result = closure.match(db.get_thread(thread.id), NOW)
    assert result.confidence >= closure.AUTO_CLOSE
    assert result.definite is False
    assert result.resolution == "needs_confirmation"    # asked, not closed


def test_closing_names_its_reasons():
    """Closing something on the user's behalf has to be defensible. A score is
    not an explanation."""
    item = make_item(text="pay the water bill")
    thread = _thread_with_items(item)
    engine.manual_close(item.id)
    assert closure.match(db.get_thread(thread.id), NOW).reasons


# ----------------------------------------------------------------- the scan
def test_scan_closes_and_records_who_closed_it():
    item = make_item(text="pay the water bill")
    thread = _thread_with_items(item)
    engine.manual_close(item.id)

    outcome = closure.scan(NOW)
    assert outcome["auto_closed"] == 1
    closed = db.get_thread(thread.id)
    assert closed.state == ThreadState.RESOLVED
    assert closed.resolved_by == "evidence"


def test_scan_never_asks_the_same_question_twice():
    thread = threads.create(title="Skating with the kids")
    threads.set_deadline(thread.id, days_from_now(-30, base=NOW), source=DeadlineSource.USER)

    assert closure.scan(NOW)["needs_confirmation"] == 1
    assert closure.scan(NOW)["needs_confirmation"] == 0
    assert len(db.pending_thread_closures()) == 1


def test_scan_leaves_resolved_threads_alone():
    item = make_item()
    thread = _thread_with_items(item)
    threads.resolve(thread.id)
    assert closure.scan(NOW)["scanned"] == 0


def test_the_poller_runs_closure_before_the_worker():
    """A thread that just closed shouldn't be worked one last time for nothing."""
    from lifeline.jobs import poller

    item = make_item(text="pay the water bill")
    thread = _thread_with_items(item)
    engine.manual_close(item.id)

    summary = poller.cycle(NOW)
    assert summary["thread_closures"]["auto_closed"] == 1
    assert db.get_thread(thread.id).state == ThreadState.RESOLVED


# ------------------------------------------------------- confirm and reject
def test_confirming_closes_the_thread(client):
    thread = threads.create(title="Skating with the kids")
    threads.set_deadline(thread.id, days_from_now(-30, base=NOW), source=DeadlineSource.USER)
    closure.scan(NOW)

    pending = client.get("/threads/closures").json()
    assert len(pending) == 1
    assert pending[0]["reasons"]

    body = client.post(f"/threads/closures/{pending[0]['id']}/confirm").json()
    assert body["state"] == "resolved"
    # Evidence made the case even though a person signed it off.
    assert body["resolved_by"] == "evidence"
    assert client.get("/threads/closures").json() == []


def test_rejecting_keeps_the_thread_and_the_rejection(client):
    """A wrong guess the user corrected is the most informative row in the
    table — it does not get deleted."""
    thread = threads.create(title="Skating with the kids")
    threads.set_deadline(thread.id, days_from_now(-30, base=NOW), source=DeadlineSource.USER)
    closure.scan(NOW)
    pending = client.get("/threads/closures").json()

    body = client.post(f"/threads/closures/{pending[0]['id']}/reject").json()
    assert body["state"] == "live"
    assert client.get("/threads/closures").json() == []
    row = db.get_thread_closure(pending[0]["id"])
    assert row["resolution"] == "rejected"


def test_confirming_twice_is_a_404(client):
    thread = threads.create(title="Skating with the kids")
    threads.set_deadline(thread.id, days_from_now(-30, base=NOW), source=DeadlineSource.USER)
    closure.scan(NOW)
    cid = client.get("/threads/closures").json()[0]["id"]
    assert client.post(f"/threads/closures/{cid}/confirm").status_code == 200
    assert client.post(f"/threads/closures/{cid}/confirm").status_code == 404


def test_the_closures_route_is_not_swallowed_by_the_thread_id_route(client):
    """FastAPI matches in declaration order, so a static segment placed after
    the parameterised one is eaten by it — this returned "no thread closures"
    for a thread whose id was literally "closures"."""
    assert client.get("/threads/closures").status_code == 200
    assert client.get("/threads/ghost").status_code == 404


# ------------------------------------------- a watcher that found what it sought
#
# The American Water thread: paid on the 22nd, due the 31st. Every item settled,
# three payment confirmations attached by the mail watcher — and the scan
# declined at 0.40, because the deadline had not passed yet and nothing in here
# read watcher evidence at all.

def _paid_bill_thread(deadline: str):
    from conftest import make_conversation, make_message
    from lifeline import db, threads
    from lifeline.models import Evidence
    from lifeline.threads import watchers as w
    from lifeline.threads.watchers import Watcher

    thread = threads.create(title="Pay the American Water bill", deadline=deadline)
    make_conversation("gmail:amwater", source="gmail", name="American Water")
    confirmation = make_message(
        "Payment Confirmation from American Water",
        conversation_id="gmail:amwater", person_id=None,
    )

    watcher = Watcher(
        thread_id=thread.id, kind=w.WatchKind.MAIL,
        spec={"sender": "amwater"}, what="American Water payment confirmation",
        cadence_minutes=180, fire_count=3,
    )
    db.save_watcher(watcher)
    db.add_evidence(Evidence(
        thread_id=thread.id, kind="message", ref_id=confirmation.id,
        note=f"watcher: {watcher.what}",
    ))
    return thread


def test_a_watcher_hit_alone_asks_rather_than_closes():
    """A watcher is precise but it is still one signal. On its own it earns a
    question, not a decision made on the user's behalf."""
    from datetime import datetime, timedelta, timezone
    from lifeline.threads import closure

    ahead = (datetime.now(timezone.utc) + timedelta(days=9)).date().isoformat()
    result = closure.match(_paid_bill_thread(ahead))

    assert result.resolution == "needs_confirmation", (result.confidence, result.reasons)
    assert any("watcher" in r for r in result.reasons), result.reasons


def test_a_paid_bill_closes_before_its_due_date():
    """The real American Water case: every claimed item settled *and* the
    watcher found the confirmation. Paying early is the point of a deadline,
    not a reason to doubt it — this scored 0.85, was clamped to 0.40 for being
    nine days early, and sat open with three confirmations attached."""
    from datetime import datetime, timedelta, timezone
    from lifeline import db
    from lifeline.models import Evidence, Item
    from lifeline.threads import closure

    ahead = (datetime.now(timezone.utc) + timedelta(days=9)).date().isoformat()
    thread = _paid_bill_thread(ahead)

    item = Item(conversation_id="gmail:amwater", raw_text="pay the bill",
                status="completed")
    db.save_item(item)
    db.add_evidence(Evidence(thread_id=thread.id, kind="item", ref_id=item.id))

    result = closure.match(thread)
    assert result.resolution == "auto_closed", (result.confidence, result.reasons)
    assert result.definite, result.reasons
    assert not any("still ahead" in r for r in result.reasons), result.reasons


def test_a_deadline_watcher_alone_never_closes_anything():
    """One firing means the date is *coming*, which is the opposite of done."""
    from datetime import datetime, timedelta, timezone
    from lifeline import db, threads
    from lifeline.models import Evidence
    from lifeline.threads import closure, watchers as w
    from lifeline.threads.watchers import Watcher

    ahead = (datetime.now(timezone.utc) + timedelta(days=9)).date().isoformat()
    thread = threads.create(title="Sign kid up for soccer", deadline=ahead)
    watcher = Watcher(thread_id=thread.id, kind=w.WatchKind.DEADLINE,
                      spec={"days_before": 3}, what="deadline approaching",
                      cadence_minutes=180, fire_count=2)
    db.save_watcher(watcher)
    db.add_evidence(Evidence(thread_id=thread.id, kind="message", ref_id="whatever",
                             note=f"watcher: {watcher.what}"))

    assert closure.match(thread).resolution is None


def test_evidence_the_thread_claimed_itself_is_not_a_watcher_hit():
    """Only evidence a watcher attached counts — otherwise every thread with
    a founding message would look confirmed."""
    from datetime import datetime, timedelta, timezone
    from lifeline import db, threads
    from lifeline.models import Evidence
    from lifeline.threads import closure

    ahead = (datetime.now(timezone.utc) + timedelta(days=9)).date().isoformat()
    thread = threads.create(title="Pay the American Water bill", deadline=ahead)
    db.add_evidence(Evidence(thread_id=thread.id, kind="message",
                             ref_id="founding-msg", role="founding"))

    assert closure.match(thread).resolution is None


def test_a_future_event_still_will_not_close_early():
    """The clamp this relaxes is real for the other kind of date — a concert
    on the 29th has not happened just because the tickets email is filed."""
    from datetime import datetime, timedelta, timezone
    from lifeline import db, threads
    from lifeline.models import Evidence, Item
    from lifeline.threads import closure

    ahead = (datetime.now(timezone.utc) + timedelta(days=9)).date().isoformat()
    thread = threads.create(title="Beres Hammond concert", deadline=ahead)
    item = Item(conversation_id="c1", raw_text="buy the tickets", status="completed")
    db.save_item(item)
    db.add_evidence(Evidence(thread_id=thread.id, kind="item", ref_id=item.id))

    result = closure.match(thread)
    assert result.resolution is None, result.reasons
    assert any("still ahead" in r for r in result.reasons), result.reasons


# ------------------------------------- the two ways a thread closed itself
def test_an_overdue_thread_is_not_closed_by_an_unrelated_text():
    """The audit's headline repro. A date more than two weeks gone scored 0.5
    and counted as definite; any text to that person added 0.35; 0.85 clears
    the auto-close band. Together they resolved a bill nobody had paid on the
    strength of a "happy birthday" sent to the same person."""
    item = make_item(text="pay the $3,095 Capital One balance")
    thread = _thread_with_items(item, title="Capital One payment")
    threads.set_deadline(thread.id, days_from_now(-30, base=NOW), source=DeadlineSource.USER)
    make_message("happy birthday!!", is_from_user=True,
                 at=datetime.fromisoformat(thread.opened_at) + timedelta(hours=1))

    result = closure.match(db.get_thread(thread.id), NOW)
    assert result.resolution != "auto_closed"
    assert result.definite is False
    # And it says which part it is unsure about.
    assert any("still open" in r for r in result.reasons)
    # The item it was tracking is untouched — the thing that must never happen
    # here is a bill marked done by a system that only saw a birthday.
    assert db.get_item(item.id).status == "pending"


def test_a_passed_date_still_closes_a_thread_whose_work_is_settled():
    """The other half of the same rule: the skating trip from May, its items
    done, its date three months gone, is genuinely over."""
    item = make_item(text="book the skating session")
    thread = _thread_with_items(item, title="Skating with the kids")
    threads.set_deadline(thread.id, days_from_now(-90, base=NOW), source=DeadlineSource.USER)
    engine.manual_close(item.id)

    result = closure.match(db.get_thread(thread.id), NOW)
    assert result.resolution == "auto_closed"
    assert result.definite is True


def test_a_rejected_argument_is_not_put_to_the_user_again():
    """"Never ask twice" was keyed on the rows the case cited, and one of those
    rows is "the user replied" — which changes every time they say anything. On
    the live database the same closure was put to the user twice in one day."""
    item = make_item(text="buy the basketball hoop for Milo")
    thread = _thread_with_items(item, title="Basketball hoop for Milo")
    threads.set_deadline(thread.id, days_from_now(-30, base=NOW), source=DeadlineSource.USER)
    make_message("still looking at the hoop options", is_from_user=True,
                 at=datetime.fromisoformat(thread.opened_at) + timedelta(hours=1))

    first = closure.scan(NOW)
    assert first["needs_confirmation"] == 1

    # They say something else about it the next day. Same argument, same parts.
    make_message("that hoop is expensive", is_from_user=True,
                 at=datetime.fromisoformat(thread.opened_at) + timedelta(days=1))
    assert closure.scan(NOW)["needs_confirmation"] == 0


def test_a_silence_thread_closes_when_the_person_actually_replied():
    """The Theo B case: his replies were ingested five days after he sent
    them (the store was blind), and the thread built on the blind spot stayed
    live until the draft writer happened to read the conversation. Judged by
    message timestamp, not ingestion time — a late-learned reply still counts
    from the moment it was sent."""
    from lifeline.models import ThreadOrigin

    founding = make_message("any word on the meter?", is_from_user=True)
    thread = threads.create(
        title="Maya went quiet", origin=ThreadOrigin.SILENCE,
        evidence=[Evidence(kind="message", ref_id=founding.id, role="founding")],
    )
    # Her reply, sent after the silence started but ingested long after.
    make_message("sorry! yes, tomorrow works",
                 at=datetime.fromisoformat(founding.timestamp) + timedelta(hours=20))

    result = closure.match(db.get_thread(thread.id), NOW)
    assert result.resolution == "auto_closed"
    assert result.definite is True
    assert any("they answered" in r for r in result.reasons)
