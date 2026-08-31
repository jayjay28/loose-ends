"""§v2 step 7 — learning that learns, and a budget that limits.

Three sub-steps, and the first is a repair job. The live database held 19,015
behaviour events of which 18,492 (97%) were one machine-generated verdict
re-logged every poll cycle, weights nudged 288 times a day for facts that never
changed, and an `observations` counter that measured writes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from lifeline import db, threads
from lifeline.api.app import app
from lifeline.models import Evidence, Finding, ThreadOrigin, ThreadState
from lifeline.notifications import interruption
from lifeline.ranking import learning

from tests.conftest import NOW, days_from_now, make_conversation, make_item, make_message, make_person

LATER = NOW + timedelta(days=400)      # past every decay horizon

# `in_quiet_hours` reads *local* time — the user's day starts when their day
# starts, not at a UTC boundary — so a push test has to pick a moment that is
# midday wherever the tests happen to run. conftest's NOW is 09:00 UTC, which
# is before dawn in the Americas and would silence every push.
AWAKE = (
    datetime.now(timezone.utc).astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def default_person():
    make_person()
    make_conversation()


@pytest.fixture
def client():
    return TestClient(app)


# ------------------------------------------------------- 7a: the repair
def test_the_same_verdict_is_logged_once_not_every_cycle():
    """The bug: 18,492 rows, one item carrying 223 copies of an unchanged
    verdict, because the pass logged unconditionally at a five-minute cadence."""
    make_item(text="something nobody has touched", at=NOW - timedelta(days=30))

    first = learning.apply_behavior_patterns(NOW)
    before = len(db.behavior_events())
    for _ in range(5):
        learning.apply_behavior_patterns(NOW)

    assert len(db.behavior_events()) == before, "a repeated verdict wrote more rows"
    assert first["changed"] >= 0


def test_a_changed_verdict_is_still_recorded():
    """Suppressing repeats must not suppress news."""
    item = make_item(text="something", at=NOW - timedelta(days=30))
    learning.apply_behavior_patterns(NOW)
    before = len(db.behavior_events())

    item.behavior_pattern = None      # pretend the verdict flipped
    db.save_item(item)
    learning.apply_behavior_patterns(NOW)

    assert len(db.behavior_events()) >= before


def test_weights_do_not_drift_when_nothing_happened():
    """`learn_from_latency` re-nudged every person every cycle whether or not a
    message had arrived — which is how one person reached 1,041 "observations"
    and saturated at 0.9996."""
    make_person("chatty", "Chatty")
    make_conversation("imessage:c", name="Chatty")
    for n in range(6):
        make_message("ping", conversation_id="imessage:c", person_id="chatty",
                     is_from_user=False, at=NOW - timedelta(days=n, hours=2))
        make_message("pong", conversation_id="imessage:c", person_id="chatty",
                     is_from_user=True, at=NOW - timedelta(days=n, hours=1))

    assert learning.learn_from_latency(NOW) >= 1
    after_first = db.get_weight("person:chatty")

    for _ in range(10):
        learning.learn_from_latency(NOW)
    assert db.get_weight("person:chatty") == after_first


def test_new_evidence_still_moves_the_weight():
    make_person("chatty", "Chatty")
    make_conversation("imessage:c", name="Chatty")
    for n in range(6):
        make_message("ping", conversation_id="imessage:c", person_id="chatty",
                     is_from_user=False, at=NOW - timedelta(days=n, hours=2))
        make_message("pong", conversation_id="imessage:c", person_id="chatty",
                     is_from_user=True, at=NOW - timedelta(days=n, hours=1))
    learning.learn_from_latency(NOW)
    first = db.get_weight("person:chatty")

    make_message("ping again", conversation_id="imessage:c", person_id="chatty",
                 is_from_user=False, at=NOW - timedelta(hours=4))
    make_message("instant", conversation_id="imessage:c", person_id="chatty",
                 is_from_user=True, at=NOW - timedelta(hours=3, minutes=55))
    assert learning.learn_from_latency(NOW) == 1
    assert db.get_weight("person:chatty") != first


def test_repair_recomputes_rather_than_crawling_back():
    """The latency evidence was always real; only its repeated application was
    wrong. So the correction sets the value the evidence supports, instead of
    letting an EMA inch back from a saturated one at rate 0.1."""
    make_person("chatty", "Chatty")
    make_conversation("imessage:c", name="Chatty")
    for n in range(6):
        make_message("ping", conversation_id="imessage:c", person_id="chatty",
                     is_from_user=False, at=NOW - timedelta(days=n, hours=2))
        make_message("pong", conversation_id="imessage:c", person_id="chatty",
                     is_from_user=True, at=NOW - timedelta(days=n, hours=1))
    db.set_weight("person:chatty", 0.9996, observations=1041)   # the corrupted state

    assert learning.repair(NOW)["people_recomputed"] == 1
    row = db.get_weight_row("person:chatty")
    assert row["value"] < 0.9996
    # The counter now means evidence, not writes.
    assert row["observations"] < 100


# --------------------------------------------------- 7b: the key space
def test_a_thread_is_keyed_on_what_it_actually_has():
    """A thread has no person and no type — the audit's point. These three are
    derivable from the thread itself; none is invented."""
    item = make_item()
    thread = threads.create(title="a loop", evidence=[Evidence(kind="item", ref_id=item.id)])
    keys = learning.thread_keys(db.get_thread(thread.id))
    assert f"thread_origin:{ThreadOrigin.USER}" in keys
    assert "thread_dated:False" in keys
    assert "thread_person:tess" in keys


def test_a_thread_spanning_several_people_has_no_person_key():
    """Guessing one would key its learning on a coin flip."""
    make_person("robbie", "Robbie")
    a = make_item(person_id="tess", person="Tess")
    b = make_item(person_id="robbie", person="Robbie")
    thread = threads.create(title="a loop", evidence=[
        Evidence(kind="item", ref_id=a.id), Evidence(kind="item", ref_id=b.id),
    ])
    assert not any(k.startswith("thread_person:") for k in
                   learning.thread_keys(db.get_thread(thread.id)))


def test_resolving_teaches_that_this_shape_mattered():
    item = make_item()
    thread = threads.create(title="a loop", evidence=[Evidence(kind="item", ref_id=item.id)])
    before = learning.thread_importance(db.get_thread(thread.id))
    threads.resolve(thread.id, by="user")
    assert learning.thread_importance(db.get_thread(thread.id)) > before


def test_evidence_closing_a_thread_teaches_nothing():
    """The world moving on is not the user caring."""
    item = make_item()
    thread = threads.create(title="a loop", evidence=[Evidence(kind="item", ref_id=item.id)])
    before = learning.thread_importance(db.get_thread(thread.id))
    threads.resolve(thread.id, by="evidence")
    assert learning.thread_importance(db.get_thread(thread.id)) == before


def test_an_unfamiliar_key_means_no_information_not_bad_news():
    """Averaged rather than multiplied: with three signals a product would let
    one unknown key drag a thread to near zero."""
    thread = threads.create(title="brand new shape")
    assert learning.thread_importance(thread) == pytest.approx(learning.THREAD_PRIOR)


# ------------------------------------------------- 7c: the interruption bar
def test_the_bar_starts_where_the_spec_says():
    assert interruption.bar(NOW) == interruption.DEFAULT_BAR


def test_quiet_raises_the_bar_and_dig_in_lowers_it():
    """"Right thread, wrong moment" is a statement about interruption. "This
    matters more than I judged" asks to be told sooner."""
    interruption.quieted(NOW)
    assert interruption.bar(NOW) == pytest.approx(0.65)
    interruption.dug_in(NOW)
    assert interruption.bar(NOW) == pytest.approx(0.55)


def test_the_bar_is_bounded_at_both_ends():
    for _ in range(30):
        interruption.quieted(NOW)
    assert interruption.bar(NOW) <= interruption.BAR_CEILING
    for _ in range(30):
        interruption.dug_in(NOW)
    assert interruption.bar(NOW) >= interruption.BAR_FLOOR


def test_the_bar_decays_back_so_one_bad_week_is_not_forever():
    for _ in range(6):
        interruption.quieted(NOW)
    raised = interruption.bar(NOW)
    assert raised > interruption.DEFAULT_BAR
    assert interruption.bar(LATER) == pytest.approx(interruption.DEFAULT_BAR)


def test_the_swipe_verbs_move_the_bar_through_the_api(client):
    thread = threads.create(title="a loop")
    before = interruption.bar()
    client.post(f"/threads/{thread.id}/quiet")
    assert interruption.bar() > before
    client.post(f"/threads/{thread.id}/dig-in")
    client.post(f"/threads/{thread.id}/dig-in")
    assert interruption.bar() < before


def test_dig_in_raises_the_threads_importance_too(client):
    thread = threads.create(title="a loop", importance=0.5)
    body = client.post(f"/threads/{thread.id}/dig-in").json()
    assert body["importance"] > 0.5


# ------------------------------------------------- 7c: the budget itself
def _finding(thread_id, importance=0.9, kind="finding"):
    saved = db.save_finding(threads.make_finding(
        thread_id, kind=kind, headline=f"headline {importance}", importance=importance
    ))
    # Stamped an hour before AWAKE rather than at wall-clock now: these tests
    # judge at AWAKE (noon), and a suite run after 6pm made every real-now
    # finding "too old to interrupt" — a time-of-day flake, not a verdict.
    return _aged(saved, hours=1, now=AWAKE)


def test_a_finding_below_the_bar_does_not_push():
    # AWAKE, not NOW: quiet hours are checked before the bar these days, and
    # 09:00 UTC is 5am locally — the bar only gets a say in daylight.
    thread = threads.create(title="a loop")
    verdict = interruption.may_interrupt(_finding(thread.id, importance=0.3), AWAKE)
    assert verdict["push"] is False
    assert "below the bar" in verdict["reason"]


def test_a_finding_above_the_bar_pushes():
    thread = threads.create(title="a loop")
    verdict = interruption.may_interrupt(_finding(thread.id, importance=0.9), AWAKE)
    assert verdict["push"] is True, verdict["reason"]


def test_nothing_findings_never_push():
    """"I looked and found nothing" is worth recording and never worth waking
    someone for."""
    thread = threads.create(title="a loop")
    f = _finding(thread.id, importance=0.99, kind="nothing")
    assert interruption.may_interrupt(f, AWAKE)["push"] is False


def test_the_daily_cap_holds():
    """A bar alone is not a budget. Ten findings clearing it in one cycle would
    send ten pushes — looser than what v1.5 already ships. The cap governs
    threads the *system* opened; ends the user declared ride over it (below)."""
    from lifeline.models import ThreadOrigin

    thread = threads.create(title="a loop", origin=ThreadOrigin.SILENCE)
    for n in range(6):
        _finding(thread.id, importance=0.9 + n / 1000)

    sent = interruption.queue_findings(AWAKE)
    assert len(sent) == interruption.MAX_PUSHES_PER_DAY


def test_an_answer_on_a_declared_end_skips_the_bar_and_the_cap():
    """§v3 — the user asked for this end by name; being told when it moves is
    the product. The bar and cap protect them from the system's judgement,
    not from their own."""
    from lifeline.models import ThreadOrigin

    noise = threads.create(title="system noise", origin=ThreadOrigin.SILENCE)
    for n in range(4):
        _finding(noise.id, importance=0.9 + n / 1000)
    declared = threads.create(title="mini golf with the kids")   # origin=user
    answer = _finding(declared.id, importance=0.5)               # under the bar

    verdict = interruption.may_interrupt(answer, AWAKE, thread=declared)
    assert verdict["push"] is True
    assert "you added" in verdict["reason"]

    sent = interruption.queue_findings(AWAKE)
    assert answer.id in sent, "under the bar, over a full cap — pushed anyway"


def test_an_answer_still_respects_quiet_hours():
    """Exempt from the system's taste, not from the clock."""
    declared = threads.create(title="a declared end")
    answer = _finding(declared.id, importance=0.99)
    verdict = interruption.may_interrupt(answer, NOW, thread=declared)  # 5am local
    assert verdict["push"] is False
    assert verdict["reason"] == "quiet hours"


def test_one_push_per_finding_ever():
    """`notifications.item_id` was the only dedupe key; a finding is not an
    item, so it needed its own column."""
    thread = threads.create(title="a loop")
    f = _finding(thread.id, importance=0.9)
    db.queue_notification("finding", "active", "t", "b", finding_id=f.id)
    assert db.notification_exists_for_finding(f.id) is True
    assert interruption.may_interrupt(f, AWAKE)["push"] is False


def _aged(finding, hours, now):
    """The same finding, written `hours` ago."""
    c = db.get_connection()
    c.execute(
        "UPDATE findings SET created_at = ? WHERE id = ?",
        ((now - timedelta(hours=hours)).isoformat(), finding.id),
    )
    c.commit()
    return db.get_finding(finding.id)


def test_a_stale_finding_is_expired_rather_than_held():
    """The cap and quiet hours defer a push; nothing expired one, so a finding
    held back on Saturday went out on Monday night with its original body. On
    the live database that meant "Assembly deadline is TODAY at 1:30 PM (7.5
    hours away)" delivered two days after the deadline, Time-Sensitive."""
    thread = threads.create(title="a loop")
    old = _aged(_finding(thread.id, importance=0.99), hours=57, now=AWAKE)

    verdict = interruption.may_interrupt(old, AWAKE)
    assert verdict["push"] is False
    assert verdict["expired"] is True
    assert "too old" in verdict["reason"]

    # And the queue retires it instead of leaving it to drain at midnight.
    assert interruption.queue_findings(AWAKE) == []
    assert db.get_finding(old.id).surfaced_at is not None
    assert db.unsurfaced_findings() == []


def test_a_fresh_finding_still_pushes():
    thread = threads.create(title="a loop")
    fresh = _aged(_finding(thread.id, importance=0.99), hours=1, now=AWAKE)
    assert interruption.may_interrupt(fresh, AWAKE)["push"] is True


def test_a_superseded_finding_never_interrupts():
    """A newer finding replaced it on the thread. Pushing the one it replaced
    is the system arguing with itself in someone's pocket."""
    thread = threads.create(title="a loop")
    f = _finding(thread.id, importance=0.99)
    c = db.get_connection()
    c.execute("UPDATE findings SET superseded_at = ? WHERE id = ?", (AWAKE.isoformat(), f.id))
    c.commit()
    assert interruption.may_interrupt(db.get_finding(f.id), AWAKE)["push"] is False


def test_quiet_hours_hold_everything_back():
    thread = threads.create(title="a loop")
    f = _finding(thread.id, importance=0.99)
    small_hours = AWAKE.astimezone().replace(hour=3).astimezone(timezone.utc)
    assert interruption.may_interrupt(f, small_hours)["reason"] == "quiet hours"


def test_the_state_is_inspectable(client):
    body = client.get("/interruption").json()
    assert body["bar"] == interruption.DEFAULT_BAR
    assert body["daily_cap"] == interruption.MAX_PUSHES_PER_DAY
    assert "pushes_today" in body


# ------------------------------- announceable urgency (interruption level)
def test_a_top_rated_finding_goes_out_time_sensitive():
    """`queue_findings` hardcoded "active" — the one level Focus silences and
    Announce Notifications skips. Nothing the worker found could ever be read
    aloud, which is the whole point of finding it while the user is busy."""
    thread = threads.create(title="a loop")
    f = _finding(thread.id, importance=0.9)
    assert interruption.level_for(f, thread, NOW) == "time_sensitive"


def test_an_ordinary_finding_stays_active():
    """Time-Sensitive breaks Focus and can be spoken aloud in a room with other
    people in it. Scarcer than "cleared the bar" on purpose."""
    thread = threads.create(title="a loop")
    f = _finding(thread.id, importance=0.7)
    assert interruption.level_for(f, thread, NOW) == "active"


def test_a_deadline_inside_a_day_makes_an_ordinary_finding_urgent():
    thread = threads.create(title="a loop", deadline=days_from_now(0.5))
    f = _finding(thread.id, importance=0.7)
    assert interruption.level_for(f, db.get_thread(thread.id), NOW) == "time_sensitive"


def test_a_passed_deadline_is_urgent_too():
    """A missed deadline is the most time-sensitive thing the system knows."""
    thread = threads.create(title="a loop", deadline=days_from_now(-3))
    f = _finding(thread.id, importance=0.7)
    assert interruption.level_for(f, db.get_thread(thread.id), NOW) == "time_sensitive"


def test_a_distant_deadline_does_not_make_it_urgent():
    thread = threads.create(title="a loop", deadline=days_from_now(30))
    f = _finding(thread.id, importance=0.7)
    assert interruption.level_for(f, db.get_thread(thread.id), NOW) == "active"


def test_the_level_reaches_the_queued_row():
    thread = threads.create(title="a loop", deadline=days_from_now(0.25))
    _finding(thread.id, importance=0.7)
    interruption.queue_findings(AWAKE)
    row = db.get_connection().execute(
        "SELECT interruption FROM notifications WHERE kind = 'finding'"
    ).fetchone()
    assert row["interruption"] == "time_sensitive"
