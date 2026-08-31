"""§v2 step 1 — the thread: an open loop in the user's head.

The tests are grouped by the promise each one protects, because the promises
are the spec's load-bearing parts: proposals stay out of the stack, the user
overrules the system, and an inferred deadline can always show its receipts.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lifeline import db, threads
from lifeline.api.app import app
from lifeline.assistant import registry
from lifeline.models import (
    DeadlineSource,
    Evidence,
    ThreadOrigin,
    ThreadState,
)
from lifeline.threads import bootstrap

from tests.conftest import days_from_now, make_conversation, make_item, make_message, make_person


@pytest.fixture(autouse=True)
def default_person():
    """`items.person_id` has a foreign key, and `make_item` defaults to Tess."""
    return make_person()


@pytest.fixture
def client():
    return TestClient(app)


# ------------------------------------------------------------- the object
def test_create_and_read_back():
    thread = threads.create(title="Puerto Rico work trip", summary="flights, agenda, kids")
    assert thread.state == ThreadState.LIVE
    assert thread.origin == ThreadOrigin.USER
    assert db.get_thread(thread.id).title == "Puerto Rico work trip"


def test_a_thread_needs_a_title():
    with pytest.raises(threads.ThreadError):
        threads.create(title="   ")


def test_resolving_records_who_closed_it():
    thread = threads.create(title="water bill")
    closed = threads.resolve(thread.id, by="user")
    assert closed.state == ThreadState.RESOLVED
    assert closed.resolved_by == "user"
    assert closed.resolved_at


def test_reopening_clears_a_stale_resolution():
    """A resolved_at left behind on a reopened thread reads as 'closed' in
    every receipt that renders it."""
    thread = threads.create(title="water bill")
    threads.resolve(thread.id)
    reopened = threads.update(thread.id, state=ThreadState.LIVE)
    assert reopened.resolved_at is None and reopened.resolved_by is None


def test_open_threads_is_live_plus_quiet():
    live = threads.create(title="live one")
    quiet = threads.create(title="quiet one", state=ThreadState.QUIET)
    threads.create(title="resolved one", state=ThreadState.RESOLVED)
    threads.create(title="a guess", state=ThreadState.PROPOSED, origin=ThreadOrigin.SYSTEM_PROPOSED)
    assert {t.id for t in db.open_threads()} == {live.id, quiet.id}


def test_stack_orders_by_deadline_before_importance():
    """A date is a fact about the world; importance is only a belief about it."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    soon = threads.create(title="soon but dull", importance=0.1)
    threads.set_deadline(soon.id, days_from_now(1, base=now), source=DeadlineSource.USER)
    threads.create(title="important, no date", importance=0.9)
    assert db.list_threads(states=ThreadState.OPEN, reference=now)[0].id == soon.id


def test_a_passed_deadline_does_not_hold_the_top_of_the_stack():
    """The bug this exists for: the first cut sorted every deadline ascending,
    so a skating trip three months gone outranked a bill due next week and
    pinned itself to slot one forever. `signals.deadline_pressure` scores a
    passed date at -0.4 — it demotes, it does not promote."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    stale = threads.create(title="skating, back in May", importance=0.9)
    threads.set_deadline(stale.id, days_from_now(-90, base=now), source=DeadlineSource.USER)
    live = threads.create(title="bill due next week", importance=0.1)
    threads.set_deadline(live.id, days_from_now(7, base=now), source=DeadlineSource.USER)
    undated = threads.create(title="no date at all", importance=0.5)

    order = [t.id for t in db.list_threads(states=ThreadState.OPEN, reference=now)]
    assert order == [live.id, undated.id, stale.id]
    # The date itself survives — it's true, and the UI can strike it through.
    assert db.get_thread(stale.id).deadline is not None


def test_passed_deadlines_rank_most_recent_first():
    """A date gone two days is more live than one gone three months."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    ancient = threads.create(title="ancient")
    threads.set_deadline(ancient.id, days_from_now(-90, base=now), source=DeadlineSource.USER)
    recent = threads.create(title="just missed")
    threads.set_deadline(recent.id, days_from_now(-2, base=now), source=DeadlineSource.USER)
    assert [t.id for t in db.list_threads(states=ThreadState.OPEN, reference=now)] == [
        recent.id, ancient.id
    ]


# ---------------------------------------------------- proposals & consent
def test_proposals_are_not_in_the_stack():
    proposal = threads.create(
        title="maybe you care about the HOA meeting",
        state=ThreadState.PROPOSED,
        origin=ThreadOrigin.SYSTEM_PROPOSED,
    )
    assert proposal.id not in {t.id for t in db.open_threads()}
    assert [t.id for t in db.list_threads(states=[ThreadState.PROPOSED])] == [proposal.id]


def test_accepting_a_proposal_is_the_only_door_into_the_stack():
    proposal = threads.create(
        title="HOA meeting", state=ThreadState.PROPOSED, origin=ThreadOrigin.SYSTEM_PROPOSED
    )
    accepted = threads.accept_proposal(proposal.id)
    assert accepted.state == ThreadState.LIVE
    assert accepted.id in {t.id for t in db.open_threads()}
    # It is no longer a proposal, so it can't be accepted twice.
    with pytest.raises(threads.ThreadError):
        threads.accept_proposal(proposal.id)


def test_dismissing_a_proposal_archives_rather_than_deletes():
    proposal = threads.create(
        title="HOA meeting", state=ThreadState.PROPOSED, origin=ThreadOrigin.SYSTEM_PROPOSED
    )
    assert threads.dismiss_proposal(proposal.id).state == ThreadState.ARCHIVED
    assert db.get_thread(proposal.id) is not None


def test_the_model_proposes_it_does_not_declare(client):
    """create_thread from a tool defaults to `proposed`, so nothing the loop
    dreams up lands on the stack without a tap."""
    tool = registry.by_name(registry.thread_tools())["create_thread"]
    result = tool.fn(title="Puerto Rico work trip")
    assert result["state"] == ThreadState.PROPOSED
    assert client.get("/threads").json()["threads"] == []
    assert len(client.get("/proposals").json()) == 1


# -------------------------------------------------------------- evidence
def test_one_item_can_serve_two_threads():
    """The reason evidence is a join table and not a column on items: a hotel
    confirmation belongs to the trip AND to this month's spending."""
    item = make_item(text="San Juan Marriott confirmation")
    trip = threads.create(title="Puerto Rico work trip")
    money = threads.create(title="August spending")
    threads.claim(trip.id, item.id)
    threads.claim(money.id, item.id)
    assert {t.id for t in db.threads_claiming("item", item.id)} == {trip.id, money.id}


def test_claiming_is_idempotent_and_keeps_the_founding_role():
    item = make_item()
    thread = threads.create(title="a loop", evidence=[Evidence(kind="item", ref_id=item.id, role="founding")])
    threads.claim(thread.id, item.id, role="claimed")
    evidence = db.thread_evidence(thread.id)
    assert len(evidence) == 1
    assert evidence[0].role == "founding"


def test_claiming_something_that_does_not_exist_fails_loudly():
    thread = threads.create(title="a loop")
    with pytest.raises(threads.ThreadError):
        threads.claim(thread.id, "no-such-item")


def test_evidence_resolves_to_something_a_human_can_read():
    make_person()
    make_conversation()
    message = make_message("the bill is due on the 31st", is_from_user=False)
    item = make_item(text="pay the water bill")
    thread = threads.create(title="water bill")
    threads.claim(thread.id, item.id)
    threads.claim(thread.id, message.id, kind="message", note="where the date came from")

    rendered = threads.evidence_for(thread.id)
    assert {e["kind"] for e in rendered} == {"item", "message"}
    assert any("water bill" in (e["text"] or "") for e in rendered)
    assert any(e["note"] == "where the date came from" for e in rendered)


def test_evidence_whose_row_vanished_becomes_a_tombstone():
    item = make_item()
    thread = threads.create(title="a loop")
    threads.claim(thread.id, item.id)
    db.get_connection().execute("DELETE FROM items WHERE id = ?", (item.id,))
    rendered = threads.evidence_for(thread.id)
    assert rendered[0]["title"] == "(missing)"      # honest, not silently dropped


# -------------------------------------------------------------- deadlines
def test_inferred_deadline_must_name_its_evidence():
    """Receipts are a product feature. A date with no source is a guess."""
    thread = threads.create(title="water bill")
    with pytest.raises(threads.ThreadError):
        threads.set_deadline(thread.id, days_from_now(10), source=DeadlineSource.INFERRED)


def test_inferred_deadline_carries_the_evidence_that_implied_it():
    item = make_item(text="Your American Water bill is ready", date=days_from_now(24))
    thread = threads.create(title="water bill")
    threads.set_deadline(
        thread.id,
        item.entities.date,
        source=DeadlineSource.INFERRED,
        evidence=[{"kind": "item", "ref_id": item.id}],
        reason="the bill states its due date",
    )
    receipt = threads.deadline_receipt(thread.id)
    assert receipt["source"] == "inferred"
    assert receipt["reason"] == "the bill states its due date"
    assert receipt["evidence"][0]["ref_id"] == item.id
    assert "American Water" in receipt["evidence"][0]["text"]
    # The row that implied the date is now the thread's evidence too, so the
    # receipt is reachable from one place.
    assert [e.ref_id for e in db.thread_evidence(thread.id)] == [item.id]


def test_inference_never_overrules_the_user():
    item = make_item(date=days_from_now(5))
    thread = threads.create(title="registration")
    threads.set_deadline(thread.id, days_from_now(30), source=DeadlineSource.USER)
    with pytest.raises(threads.ThreadError):
        threads.set_deadline(
            thread.id, item.entities.date, source=DeadlineSource.INFERRED,
            evidence=[{"kind": "item", "ref_id": item.id}],
        )
    with pytest.raises(threads.ThreadError):
        threads.set_deadline(thread.id, None, source=DeadlineSource.INFERRED)
    assert db.get_thread(thread.id).deadline == days_from_now(30)


def test_the_user_always_overrules_the_system():
    item = make_item(date=days_from_now(5))
    thread = threads.create(title="registration")
    threads.set_deadline(
        thread.id, item.entities.date, source=DeadlineSource.INFERRED,
        evidence=[{"kind": "item", "ref_id": item.id}],
    )
    corrected = threads.set_deadline(thread.id, days_from_now(2), source=DeadlineSource.USER)
    assert corrected.deadline == days_from_now(2)
    assert corrected.deadline_source == "user"
    assert corrected.deadline_evidence == []


def test_deadline_evidence_must_exist():
    thread = threads.create(title="a loop")
    with pytest.raises(threads.ThreadError):
        threads.set_deadline(
            thread.id, days_from_now(3), source=DeadlineSource.INFERRED,
            evidence=[{"kind": "item", "ref_id": "ghost"}],
        )


# ---------------------------------------------------------------- promote
def test_promoting_an_item_keeps_it_as_founding_evidence():
    item = make_item(text="book the flights to San Juan")
    thread = threads.promote_item(item.id)
    assert thread.origin == ThreadOrigin.PROMOTED
    assert thread.state == ThreadState.LIVE
    evidence = db.thread_evidence(thread.id)
    assert [(e.kind, e.ref_id, e.role) for e in evidence] == [("item", item.id, "founding")]
    # The item is not consumed — it stays exactly what it was.
    assert db.get_item(item.id).status == "pending"


def test_promoting_carries_the_items_date_as_an_inferred_deadline():
    item = make_item(text="soccer registration closes", date=days_from_now(28))
    thread = threads.promote_item(item.id)
    assert thread.deadline == days_from_now(28)
    assert thread.deadline_source == "inferred"
    assert thread.deadline_evidence == [{"kind": "item", "ref_id": item.id}]


def test_promoting_twice_does_not_split_the_loop():
    item = make_item()
    first = threads.promote_item(item.id)
    assert threads.promote_item(item.id).id == first.id


# ----------------------------------------------------------------- search
def test_search_finds_a_thread_through_its_evidence():
    """The case the spec is built around: a hotel confirmation says "San Juan",
    not "Puerto Rico work trip"."""
    item = make_item(text="Your Upcoming Stay at San Juan Marriott Resort")
    trip = threads.create(title="Puerto Rico work trip")
    threads.claim(trip.id, item.id)
    threads.create(title="water bill")

    hits = threads.search("San Juan Marriott")
    assert [h["thread_id"] for h in hits] == [trip.id]


def test_search_prefers_a_title_match():
    a = threads.create(title="water bill", summary="")
    item = make_item(text="mentions the water bill in passing")
    b = threads.create(title="something else")
    threads.claim(b.id, item.id)
    assert threads.search("water bill")[0]["thread_id"] == a.id


def test_search_with_no_query_lists_the_open_stack():
    threads.create(title="one")
    threads.create(title="two", state=ThreadState.PROPOSED, origin=ThreadOrigin.SYSTEM_PROPOSED)
    assert len(threads.search()) == 1
    assert len(threads.search(state="proposed")) == 1


def test_read_thread_state_is_a_read_not_a_write():
    """The audit's point: tier 4 was all writes, so the worker loop could never
    see what it already knew."""
    item = make_item(text="flights are booked")
    thread = threads.create(title="Puerto Rico work trip")
    threads.claim(thread.id, item.id)
    state = threads.read_state(thread.id)
    assert state["title"] == "Puerto Rico work trip"
    assert state["evidence_count"] == 1
    assert "flights are booked" in state["evidence"][0]["text"]
    # Named empty, not absent — findings and watchers arrive in steps 4 and 6.
    assert state["findings"] == [] and state["watchers"] == []


# ------------------------------------------------------------------ tools
def test_thread_tools_expose_two_reads():
    names = {t.name for t in registry.thread_tools()}
    assert names == {
        "create_thread", "update_thread", "set_deadline",
        "read_thread_state", "search_threads",
    }


def test_set_deadline_tool_refuses_a_bare_guess():
    thread = threads.create(title="a loop")
    tool = registry.by_name(registry.thread_tools())["set_deadline"]
    out = registry.execute(tool, {"thread_id": thread.id, "date": days_from_now(3)})
    assert "error" in out and "evidence" in out


def test_conversation_tools_no_longer_say_thread():
    """Step 0 leftover: the model reads these names, and at step 4 it would get
    `read_conversation` and `read_thread_state` in one list meaning different
    objects."""
    names = {t.name for t in registry.READ_TOOLS + registry.draft_tools()}
    assert "read_conversation" in names and "quiet_conversations" in names
    assert "read_thread" not in names and "quiet_threads" not in names

    from lifeline.assistant.tools import CONVERSE_SYSTEM
    assert "quiet_conversations" in CONVERSE_SYSTEM
    assert "read_conversation" in CONVERSE_SYSTEM
    assert "quiet_threads" not in CONVERSE_SYSTEM
    assert "read_thread " not in CONVERSE_SYSTEM


# --------------------------------------------------------------- endpoints
def test_endpoint_lifecycle(client):
    created = client.post("/threads", json={"title": "Puerto Rico work trip"}).json()
    assert created["state"] == "live"

    stack = client.get("/threads").json()
    assert [t["id"] for t in stack["threads"]] == [created["id"]]
    assert stack["running"] == 1

    patched = client.patch(f"/threads/{created['id']}", json={"importance": 0.9, "state": "quiet"}).json()
    assert patched["importance"] == 0.9 and patched["state"] == "quiet"

    resolved = client.post(f"/threads/{created['id']}/resolve").json()
    assert resolved["state"] == "resolved" and resolved["resolved_by"] == "user"
    # Resolved threads ride along for a day, struck through — the pile has to
    # be seen to shrink — but they stop counting as running.
    after = client.get("/threads").json()
    assert [t["id"] for t in after["threads"]] == [created["id"]]
    assert after["threads"][0]["lane"] == "done"
    assert after["running"] == 0


def test_endpoint_rejects_a_proposal_in_the_stack(client):
    assert client.get("/threads", params={"state": "proposed"}).status_code == 400
    thread = client.post("/threads", json={"title": "x"}).json()
    assert client.patch(f"/threads/{thread['id']}", json={"state": "proposed"}).status_code == 400


def test_endpoint_promote_returns_the_receipt(client):
    item = make_item(text="soccer registration closes", date=days_from_now(28))
    body = client.post(f"/items/{item.id}/promote").json()
    assert body["thread"]["deadline"]["source"] == "inferred"
    assert body["thread"]["deadline"]["evidence"][0]["ref_id"] == item.id
    assert body["evidence"][0]["role"] == "founding"


def test_endpoint_promote_404s_on_an_unknown_item(client):
    assert client.post("/items/nope/promote").status_code == 404


def test_endpoint_deadline_override(client):
    item = make_item(date=days_from_now(5))
    thread = client.post("/threads", json={"title": "registration"}).json()
    inferred = client.post(
        f"/threads/{thread['id']}/deadline",
        json={"date": days_from_now(5), "source": "inferred",
              "evidence": [{"kind": "item", "ref_id": item.id}]},
    ).json()
    assert inferred["deadline"]["source"] == "inferred"

    user = client.post(
        f"/threads/{thread['id']}/deadline", json={"date": days_from_now(2), "source": "user"}
    ).json()
    assert user["deadline"]["date"] == days_from_now(2)

    # And the system may not take it back.
    again = client.post(
        f"/threads/{thread['id']}/deadline",
        json={"date": days_from_now(5), "source": "inferred",
              "evidence": [{"kind": "item", "ref_id": item.id}]},
    )
    assert again.status_code == 400


def test_endpoint_evidence_claim_and_unclaim(client):
    item = make_item()
    thread = client.post("/threads", json={"title": "a loop"}).json()
    body = client.post(f"/threads/{thread['id']}/evidence", json={"ref_id": item.id}).json()
    assert body["thread"]["evidence_count"] == 1
    client.delete(f"/threads/{thread['id']}/evidence/{item.id}")
    assert client.get(f"/threads/{thread['id']}").json()["thread"]["evidence_count"] == 0


def test_endpoint_proposal_flow(client):
    proposal = threads.create(
        title="HOA meeting", state=ThreadState.PROPOSED, origin=ThreadOrigin.SYSTEM_PROPOSED
    )
    assert client.get("/threads").json()["threads"] == []
    assert len(client.get("/proposals").json()) == 1
    accepted = client.post(f"/proposals/{proposal.id}/accept").json()
    assert accepted["state"] == "live"
    assert client.get("/proposals").json() == []
    assert client.post(f"/proposals/{proposal.id}/accept").status_code == 400


def test_endpoint_404s(client):
    assert client.get("/threads/ghost").status_code == 404
    assert client.patch("/threads/ghost", json={"title": "x"}).status_code == 404
    assert client.post("/threads/ghost/resolve").status_code == 404


# --------------------------------------------------------------- bootstrap
def test_bootstrap_threads_only_open_items():
    make_item(text="pay the water bill", status="pending")
    make_item(text="already handled this one", status="completed")
    make_item(text="told it to go away", status="dismissed")

    result = bootstrap.run()
    assert result["items"] == 1
    assert result["threads"] == 1
    # Closed items stay evidence, which is what they are.
    assert db.open_threads()[0].title == "Handle pay the water bill"


def test_bootstrap_splits_one_person_by_topic():
    """Five open items from one person are rarely one loop."""
    make_person("robbie", "Robbie")
    make_item(person_id="robbie", person="Robbie", text="kids investment account paperwork")
    make_item(person_id="robbie", person="Robbie", text="the investment account minimum")
    make_item(person_id="robbie", person="Robbie", text="Beenie Man concert tickets")

    clusters = bootstrap.cluster_deterministic(db.open_items())
    assert len(clusters) == 2
    sizes = sorted(len(c["item_ids"]) for c in clusters)
    assert sizes == [1, 2]


def test_bootstrap_is_idempotent():
    make_item(text="pay the water bill")
    assert bootstrap.run()["threads"] == 1
    assert bootstrap.run()["threads"] == 0
    assert len(db.open_threads()) == 1


def test_bootstrap_carries_the_soonest_upcoming_deadline_with_its_receipt():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    make_person("robbie", "Robbie")
    late = make_item(person_id="robbie", person="Robbie", text="renew the passport soon",
                     date=days_from_now(40, base=now))
    soon = make_item(person_id="robbie", person="Robbie", text="renew the passport now",
                     date=days_from_now(4, base=now))
    bootstrap.run()
    thread = db.open_threads()[0]
    assert thread.deadline == soon.entities.date
    assert thread.deadline_evidence == [{"kind": "item", "ref_id": soon.id}]
    assert late.id in {e.ref_id for e in db.thread_evidence(thread.id)}


def test_bootstrap_skips_a_passed_date_in_favour_of_an_upcoming_one():
    """Taking the minimum date guarantees picking the most stale one — which
    is exactly what happened on the live database."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    make_person("robbie", "Robbie")
    make_item(person_id="robbie", person="Robbie", text="skating trip with the kids",
              date=days_from_now(-90, base=now))
    upcoming = make_item(person_id="robbie", person="Robbie", text="skating rink membership renewal",
                         date=days_from_now(10, base=now))
    bootstrap.run()
    thread = db.open_threads()[0]
    assert thread.deadline == upcoming.entities.date


def test_bootstrap_falls_back_to_the_most_recent_past_date():
    """All dates gone: still record one — the loop had a date and it passed,
    which is real information — just take the least stale."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    make_person("robbie", "Robbie")
    make_item(person_id="robbie", person="Robbie", text="skating trip with the kids",
              date=days_from_now(-90, base=now))
    recent = make_item(person_id="robbie", person="Robbie", text="skating rink membership lapsed",
                       date=days_from_now(-5, base=now))
    bootstrap.run()
    thread = db.open_threads()[0]
    assert thread.deadline == recent.entities.date


def test_bootstrap_dry_run_writes_nothing():
    make_item(text="pay the water bill")
    result = bootstrap.run(dry_run=True)
    assert result["threads"] == 1
    assert db.open_threads() == []


def test_bootstrap_llm_pass_falls_back_when_unavailable():
    """The LLM pass is the default, but offline (the test default) there is no
    provider, so the deterministic pass carries it and nothing is lost."""
    make_item(text="pay the water bill")
    assert bootstrap.cluster_llm(db.open_items()) is None
    result = bootstrap.run()
    assert result["clusterer"] == "deterministic"
    assert result["threads"] == 1


def test_bootstrap_llm_pass_is_rejected_when_it_drops_work(monkeypatch):
    """A clusterer that loses the user's open items is worse than none."""
    make_item(text="pay the water bill")
    make_item(text="call the vet back")
    monkeypatch.setattr(
        "lifeline.extraction.providers.run",
        lambda call, what: '{"threads": [{"title": "one", "item_ids": ["ghost"]}]}',
    )
    assert bootstrap.cluster_llm(db.open_items()) is None
    assert bootstrap.run()["clusterer"] == "deterministic"


def test_bootstrap_uses_the_llm_clusters_when_they_are_complete(monkeypatch):
    """The pass earns its place on titles: 'Your Upcoming Stay at San Juan
    Marriott Resort & Stellaris Casino' is the sender's subject line, not the
    loop the user is carrying."""
    import json as _json

    a = make_item(text="Your Upcoming Stay at San Juan Marriott Resort")
    b = make_item(text="flight to SJU confirmed")
    payload = _json.dumps({"threads": [
        {"title": "Puerto Rico work trip", "summary": "hotel + flight",
         "item_ids": [a.id, b.id]},
    ]})
    monkeypatch.setattr("lifeline.extraction.providers.run", lambda call, what: payload)

    result = bootstrap.run()
    assert result["clusterer"] == "llm"
    thread = db.open_threads()[0]
    assert thread.title == "Puerto Rico work trip"
    assert len(db.thread_evidence(thread.id)) == 2


# ------------------------------------------------------------- the bug fix
def test_calendar_sources_appear_in_ask_receipts():
    """`search_calendar` returns `summary`; `Context.sources` and
    `build_prompt` read `title`. The KeyError was swallowed by providers.run's
    bare except, so the RAG-lite fallback had never actually run."""
    from datetime import datetime, timezone

    from lifeline.assistant import tools
    from lifeline.models import CalendarEvent

    # `search_calendar` is upcoming-only against wall-clock today.
    soon = days_from_now(3, base=datetime.now(timezone.utc))
    db.upsert_calendar_events([
        CalendarEvent(id="e1", calendar_id="c", summary="Flight to San Juan", start_at=soon)
    ])
    ctx = tools.gather_context("what's my flight?")
    assert "calendar: Flight to San Juan" in ctx.sources()
    assert "Flight to San Juan" in tools.build_prompt("what's my flight?", ctx)


# ------------------------------------------- §v3 Loose Ends — the reason chip
def test_the_reason_chip_says_the_loudest_true_thing():
    """`why` is each pressure term made legible, one per card, loudest first.
    A card that moves without saying which force moved it reads as a shuffle."""
    from tests.conftest import NOW

    overdue = threads.create(title="water bill", deadline=days_from_now(-2))
    assert threads.why(overdue, NOW) == {"kind": "overdue", "text": "overdue"}

    due = threads.create(title="school run", deadline=days_from_now(3))
    chip = threads.why(due, NOW)
    assert chip["kind"] == "due" and chip["text"].startswith("due ")

    # A staged move outranks the declaration floor…
    moved = threads.create(title="pajama order")
    db.save_finding(threads.make_finding(moved.id, kind="action",
                                         headline="draft ready"))
    assert threads.why(moved, NOW) == {"kind": "move", "text": "move ready"}

    # …but a bare fresh declaration says so.
    fresh = threads.create(title="mini golf")
    assert threads.why(fresh, NOW) == {"kind": "new", "text": "new today"}

    # Quiet cards say nothing — the band already does.
    hushed = threads.create(title="hushed")
    threads.quiet(hushed.id)
    assert threads.why(db.get_thread(hushed.id), NOW) is None

    resolved = threads.create(title="done thing")
    threads.resolve(resolved.id)
    assert threads.why(db.get_thread(resolved.id), NOW) == {"kind": "tied",
                                                            "text": "closed"}


def test_the_reason_chip_rides_the_stack(client=None):
    """`why_kind`/`why_text` travel on /threads, decided server-side like tier."""
    client = TestClient(app)
    threads.create(title="water bill", deadline=days_from_now(-1))
    rows = client.get("/threads").json()["threads"]
    chips = {r["title"]: (r["why_kind"], r["why_text"]) for r in rows}
    assert chips["water bill"] == ("overdue", "overdue")
