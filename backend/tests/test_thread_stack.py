"""§v2 step 2 — the stack, as the main view renders it.

The lane's appearance is decided on the server. `Theme.swift` already states
the rule for interruption levels ("driven entirely by the server's interruption
level so the client never re-decides urgency"), and a thread's urgency obeys it
too — so these tests are the spec for what the UI draws.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from lifeline import db, threads
from lifeline.api.app import app
from lifeline.models import DeadlineSource, Evidence, ThreadOrigin, ThreadState
from lifeline.threads import LaneState

from tests.conftest import days_from_now, make_item, make_person

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def default_person():
    return make_person()


@pytest.fixture
def client():
    return TestClient(app)


# ------------------------------------------------------------ the stripe
def test_lane_state_maps_the_mockup():
    """ember needs you today · gold has a deadline · teal is live · grey is
    idle · olive is closed."""
    hot = threads.create(title="due tomorrow")
    threads.set_deadline(hot.id, days_from_now(0.5, base=NOW), source=DeadlineSource.USER)
    warm = threads.create(title="due next month")
    threads.set_deadline(warm.id, days_from_now(30, base=NOW), source=DeadlineSource.USER)
    live = threads.create(title="running, no date")
    idle = threads.create(title="told to hush", state=ThreadState.QUIET)
    done = threads.create(title="finished")
    threads.resolve(done.id)

    assert threads.lane_state(db.get_thread(hot.id), NOW) == LaneState.HOT
    assert threads.lane_state(db.get_thread(warm.id), NOW) == LaneState.WARM
    assert threads.lane_state(db.get_thread(live.id), NOW) == LaneState.LIVE
    assert threads.lane_state(db.get_thread(idle.id), NOW) == LaneState.IDLE
    assert threads.lane_state(db.get_thread(done.id), NOW) == LaneState.DONE


def test_a_just_passed_deadline_is_still_hot_but_an_old_one_is_not():
    """`signals.deadline_pressure` treats a date up to a day past as "due
    within 24 hours" and anything older as -0.4. The stripe follows it."""
    yesterday = threads.create(title="was due yesterday")
    threads.set_deadline(yesterday.id, days_from_now(-0.5, base=NOW), source=DeadlineSource.USER)
    ancient = threads.create(title="was due in May")
    threads.set_deadline(ancient.id, days_from_now(-90, base=NOW), source=DeadlineSource.USER)

    assert threads.lane_state(db.get_thread(yesterday.id), NOW) == LaneState.HOT
    # The date is gone. It is not pressure, and it does not get the ember stripe.
    assert threads.lane_state(db.get_thread(ancient.id), NOW) == LaneState.LIVE


# ------------------------------------------------- resolution stays visible
def test_a_resolved_thread_rides_along_for_a_day():
    """"Resolution should be visible, not vanish." A pile that only ever grows
    is the failure this product exists to avoid."""
    done = threads.create(title="paid the water bill")
    threads.resolve(done.id)
    threads.create(title="still open")

    stack = threads.stack(NOW)
    assert [t.title for t in stack] == ["still open", "paid the water bill"]
    assert threads.lane_state(stack[-1], NOW) == LaneState.DONE


def test_resolved_threads_archive_themselves_after_their_day():
    done = threads.create(title="paid the water bill")
    # Stamped from the frozen clock, not the wall clock — otherwise the
    # sweep below compares a real timestamp against a fabricated cutoff and
    # the result depends on what time of day the suite runs.
    threads.resolve(done.id, reference=NOW)

    # Same day: still on the stack, nothing archived.
    assert threads.sweep_resolved(NOW) == 0
    assert len(threads.stack(NOW)) == 1

    later = NOW + timedelta(hours=threads.RESOLVED_VISIBLE_HOURS + 1)
    assert threads.sweep_resolved(later) == 1
    assert db.get_thread(done.id).state == ThreadState.ARCHIVED
    assert threads.stack(later) == []


def test_the_poller_archives_stale_resolutions():
    from lifeline.jobs import poller

    done = threads.create(title="done and dusted")
    threads.resolve(done.id)
    later = datetime.now(timezone.utc) + timedelta(hours=threads.RESOLVED_VISIBLE_HOURS + 1)
    assert poller.cycle(later)["threads_archived"] == 1


# --------------------------------------------------------- the NEW badge
def test_founding_evidence_is_not_news():
    """The badge counts arrivals, not the thread's own contents. Counting every
    row on an unopened thread made the header read "15 running · 15 need you"
    on the real database — true, and worth nothing."""
    item = make_item(text="the first thing")
    thread = threads.create(title="a loop", evidence=[Evidence(kind="item", ref_id=item.id)])
    assert threads.unseen_count(db.get_thread(thread.id)) == 0


def test_unseen_counts_evidence_that_arrived_since_you_looked():
    item = make_item(text="the first thing")
    thread = threads.create(title="a loop", evidence=[Evidence(kind="item", ref_id=item.id)])
    thread.opened_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    db.save_thread(thread)

    later = make_item(text="something that arrived after")
    db.add_evidence(Evidence(thread_id=thread.id, kind="item", ref_id=later.id))
    # Both rows now postdate the (backdated) opening.
    assert threads.unseen_count(db.get_thread(thread.id)) == 2

    # Opening it clears the badge.
    threads.mark_seen(thread.id)
    assert threads.unseen_count(db.get_thread(thread.id)) == 0


def test_seen_endpoint_clears_the_badge(client):
    """The realistic shape: a thread opened a while back, evidence that landed
    since, and the user opening it now."""
    item = make_item()
    thread = threads.create(title="a loop")
    thread.opened_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    db.save_thread(thread)
    db.add_evidence(Evidence(thread_id=thread.id, kind="item", ref_id=item.id))

    assert client.get("/threads").json()["threads"][0]["unseen"] == 1
    assert client.post(f"/threads/{thread.id}/seen").json()["unseen"] == 0
    assert client.get("/threads").json()["threads"][0]["unseen"] == 0


# ------------------------------------------------------------ the header
def test_counts_running_and_needs_you():
    hot = threads.create(title="due today")
    threads.set_deadline(hot.id, days_from_now(0.5, base=NOW), source=DeadlineSource.USER)
    threads.create(title="just running")
    resolved = threads.create(title="done")
    threads.resolve(resolved.id)

    tally = threads.counts(NOW)
    assert tally["running"] == 2        # resolved doesn't count as carried
    assert tally["needs_you"] == 1      # only the hot one


# ------------------------------------------------------------- the lane
def test_lane_payload_has_everything_the_row_draws(client):
    item = make_item(text="Your American Water bill is ready")
    thread = threads.create(
        title="Pay the water bill",
        summary="Bill arrived — payment still outstanding",
        evidence=[Evidence(kind="item", ref_id=item.id)],
    )
    # The endpoint reads the wall clock, so the date has to be future *now* —
    # conftest's `days_from_now` anchors to a fixed NOW that is already past.
    threads.set_deadline(
        thread.id, days_from_now(3, base=datetime.now(timezone.utc)),
        source=DeadlineSource.INFERRED,
        evidence=[{"kind": "item", "ref_id": item.id}], reason="the bill states its due date",
    )
    lane = client.get("/threads").json()["threads"][0]
    assert lane["title"] == "Pay the water bill"
    assert lane["subtitle"] == "Bill arrived — payment still outstanding"
    assert lane["lane"] == LaneState.WARM
    assert lane["unseen"] == 0        # founding evidence isn't news
    assert lane["deadline"]["source"] == "inferred"
    assert [m["kind"] for m in lane["activity"]] == ["evidence"]


def test_subtitle_falls_back_to_the_latest_evidence():
    """A lane with an empty second line is a broken row — never ship one."""
    item = make_item(text="Reply YES for more info")
    thread = threads.create(title="a loop with no summary",
                            evidence=[Evidence(kind="item", ref_id=item.id)])
    assert "YES" in (threads.subtitle(db.get_thread(thread.id)) or "")


def test_a_thread_with_nothing_at_all_has_no_subtitle():
    thread = threads.create(title="just declared, nothing attached")
    assert threads.subtitle(db.get_thread(thread.id)) is None


def test_stack_endpoint_carries_the_header_counts(client):
    threads.create(title="one")
    threads.create(title="two", state=ThreadState.QUIET)
    body = client.get("/threads").json()
    assert body["running"] == 2
    assert body["generated_at"]
    assert len(body["threads"]) == 2


# ------------------------------------------------- the declaration floor
#
# What a thread scores in its first day, and why. Before this, `pressure`
# was built entirely out of things that happen *to* a thread — a date
# arriving, the worker staging a move — so the one moment a person stops
# and types what they are carrying scored 0.0 and landed in the index.

def declared(title="book the flights", **kw):
    """A thread the user declared themselves, opened at NOW."""
    thread = threads.create(title=title, origin=ThreadOrigin.USER, **kw)
    thread.opened_at = NOW.isoformat(timespec="seconds")
    db.save_thread(thread)
    return db.get_thread(thread.id)


def test_a_thread_you_just_declared_is_at_least_a_brief():
    """The bug this floor exists for: no deadline, no findings, nothing
    learned — the old score was 0.0, which is the bottom of the page."""
    thread = declared()
    assert threads.pressure(thread, NOW) >= 0.30
    assert threads.tiers([thread], NOW)[thread.id] == threads.Tier.BRIEF


def test_the_floor_holds_above_the_brief_threshold_all_day():
    """A decaying *term* would drop under 0.30 minutes after you typed it,
    which is the failure the floor is shaped to avoid."""
    thread = declared()
    for hours in (0, 1, 12, 23.9):
        at = NOW + timedelta(hours=hours)
        assert threads.pressure(thread, at) >= 0.30, hours
        assert threads.tiers([thread], at)[thread.id] == threads.Tier.BRIEF


def test_the_floor_decays_so_todays_declarations_order_by_recency():
    thread = declared()
    fresh = threads.pressure(thread, NOW)
    later = threads.pressure(thread, NOW + timedelta(hours=12))
    assert fresh > later >= 0.30


def test_the_floor_expires_after_a_day():
    """Saying it is worth something. It stops being worth something
    tomorrow — after that the thread ranks on its merits like everything
    else."""
    thread = declared()
    at = NOW + timedelta(hours=25)
    assert threads.pressure(thread, at) == 0.0
    assert threads.tiers([thread], at)[thread.id] == threads.Tier.INDEX


def test_declaring_something_cannot_take_the_lead_from_an_overdue_thread():
    """The floor buys a place above the fold, never the top of the page."""
    overdue = threads.create(title="passport renewal")
    threads.set_deadline(overdue.id, days_from_now(-2, base=NOW), source=DeadlineSource.USER)
    overdue = db.get_thread(overdue.id)
    fresh = declared()

    assigned = threads.tiers([fresh, overdue], NOW)
    assert assigned[overdue.id] == threads.Tier.LEAD
    assert assigned[fresh.id] == threads.Tier.BRIEF


def test_the_floor_never_lowers_a_score_the_thread_earned():
    """It is a floor, not an override — a thread you declared *and* that is
    overdue keeps the deadline's weight."""
    thread = declared()
    threads.set_deadline(thread.id, days_from_now(-1, base=NOW), source=DeadlineSource.USER)
    thread = db.get_thread(thread.id)
    assert threads.pressure(thread, NOW) >= 0.55


def test_only_the_user_declaring_earns_the_floor():
    """The system opening a thread on your behalf is not you saying it."""
    swept = threads.create(title="nobody replied to you", origin=ThreadOrigin.SILENCE)
    swept.opened_at = NOW.isoformat(timespec="seconds")
    db.save_thread(swept)
    assert threads.pressure(db.get_thread(swept.id), NOW) == 0.0


def test_quieting_what_you_declared_puts_it_back_down():
    """"Right thread, wrong moment" has to win over "but you typed it"."""
    thread = declared()
    threads.quiet(thread.id)
    assert threads.pressure(db.get_thread(thread.id), NOW) == 0.0


def test_declaring_returns_the_tier_it_will_actually_occupy(client):
    """The client animates the row into its destination, so the create
    response has to name that destination. It used to come back with the
    schema default and jump a tier on the next reload."""
    body = client.post("/threads", json={"title": "book the flights"}).json()
    assert body["tier"] == threads.Tier.BRIEF
    assert body["pressure"] >= 0.30
