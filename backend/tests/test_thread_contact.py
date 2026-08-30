"""§v2 — who to contact when a thread needs a message.

From the field notes: "when you add a thread to the lineup, you should be able
to attach a person to it, if it needs to contact someone attached to the
thread, it knows who to contact."

The `Thread` docstring rules out a `person_id`, and rightly — a thread is a
goal, not a message about someone, and "Pay the American Water bill" concerns
nobody. This is the narrower thing: the *counterpart*, the person the system
would write to on the user's behalf. Most threads have none. A thread the user
declares themselves has no evidence at all, which is the case that was broken:
the writer had nobody to address and had to invent a recipient.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lifeline import db, threads
from lifeline.api.app import app
from lifeline.assistant import registry
from lifeline.models import Evidence

from tests.conftest import make_conversation, make_item, make_person


@pytest.fixture(autouse=True)
def default_person():
    make_person()
    make_conversation()


@pytest.fixture
def client():
    return TestClient(app)


# ------------------------------------------------------------- the column
def test_a_thread_has_no_contact_by_default():
    """Most threads are about no one. The field stays empty rather than
    guessing a person from thin air."""
    thread = threads.create(title="Pay the American Water bill")
    assert thread.contact_person_id is None


def test_a_contact_can_be_attached_at_creation():
    thread = threads.create(title="ask Maya about the trip", contact_person_id="maya")
    assert db.get_thread(thread.id).contact_person_id == "maya"


def test_an_unknown_person_is_refused_at_creation():
    """A contact that doesn't resolve is worse than none — the writer would
    take it as an instruction and draft to nobody."""
    with pytest.raises(threads.ThreadError):
        threads.create(title="a loop", contact_person_id="nobody")


def test_a_contact_can_be_set_and_cleared_later():
    thread = threads.create(title="a loop")
    assert threads.set_contact(thread.id, "maya").contact_person_id == "maya"
    assert threads.set_contact(thread.id, None).contact_person_id is None


def test_setting_an_unknown_contact_is_refused():
    thread = threads.create(title="a loop")
    with pytest.raises(threads.ThreadError):
        threads.set_contact(thread.id, "nobody")


# ------------------------------------------------------- the drafting brief
def test_a_declared_thread_with_a_contact_has_someone_to_write_to():
    """The actual bug. Without evidence there were no people in the brief at
    all, so the writer was asked to draft a message to no one."""
    bare = threads.create(title="chase the plumber")
    assert threads.draft_brief(bare.id)["people"] == []

    named = threads.create(title="chase the plumber", contact_person_id="maya")
    people = threads.draft_brief(named.id)["people"]
    assert [p["person_id"] for p in people] == ["maya"]
    assert people[0]["is_the_contact"] is True


def test_the_contact_comes_first_among_several_people():
    """A thread can touch several people. The writer shouldn't have to guess
    which one the user meant."""
    make_person("bobby", "Bobby")
    item = make_item(person_id="bobby", person="Bobby")
    thread = threads.create(
        title="a loop",
        evidence=[Evidence(kind="item", ref_id=item.id)],
        contact_person_id="maya",
    )
    people = threads.draft_brief(thread.id)["people"]
    assert people[0]["person_id"] == "maya"
    assert people[0]["is_the_contact"] is True
    assert {p["person_id"] for p in people} == {"maya", "bobby"}


def test_evidence_people_are_not_marked_as_the_contact():
    item = make_item()
    thread = threads.create(title="a loop", evidence=[Evidence(kind="item", ref_id=item.id)])
    assert threads.draft_brief(thread.id)["people"][0]["is_the_contact"] is False


# ------------------------------------------------------------ the tool
def test_draft_message_falls_back_to_the_thread_contact():
    """The payoff: "it knows who to contact". The model no longer has to
    supply a person_id it has no way to know."""
    thread = threads.create(title="a loop", contact_person_id="maya")
    drafted = []
    tool = registry.by_name(registry.draft_tools(drafted, thread=db.get_thread(thread.id)))["draft_message"]
    result = tool.fn(text="hey, still on for Friday?")

    assert result["drafted"] is True
    assert drafted[0]["person_id"] == "maya"


def test_an_explicit_person_still_wins_over_the_contact():
    make_person("bobby", "Bobby")
    thread = threads.create(title="a loop", contact_person_id="maya")
    drafted = []
    tool = registry.by_name(registry.draft_tools(drafted, thread=db.get_thread(thread.id)))["draft_message"]
    tool.fn(person_id="bobby", text="hi")
    assert drafted[0]["person_id"] == "bobby"


def test_drafting_without_a_person_or_a_contact_says_so():
    """Rather than raising a TypeError the loop would have to parse out of a
    stack trace."""
    thread = threads.create(title="a loop")
    tool = registry.by_name(registry.draft_tools([], thread=db.get_thread(thread.id)))["draft_message"]
    assert "error" in tool.fn(text="hi")


def test_the_worker_gets_the_contact_aware_tool():
    """`scoped_for` builds the tool set per thread, so the fallback has to be
    bound there rather than at the flat global registry."""
    thread = threads.create(title="a loop", contact_person_id="maya")
    drafted = []
    tools = registry.scoped_for(db.get_thread(thread.id))
    assert "draft_message" in {t.name for t in tools}


# ------------------------------------------------------------- the wire
def test_the_contact_round_trips_through_the_api(client):
    thread = threads.create(title="a loop")
    body = client.post(f"/threads/{thread.id}/contact", json={"person_id": "maya"}).json()
    assert body["contact_person_id"] == "maya"
    # Resolved for display, so a lane doesn't need a second round trip.
    assert body["contact_name"] == "Maya"


def test_the_contact_can_be_cleared_through_the_api(client):
    thread = threads.create(title="a loop", contact_person_id="maya")
    body = client.post(f"/threads/{thread.id}/contact", json={"person_id": None}).json()
    assert body["contact_person_id"] is None
    assert body["contact_name"] is None


def test_an_unknown_contact_is_refused_by_the_api(client):
    thread = threads.create(title="a loop")
    r = client.post(f"/threads/{thread.id}/contact", json={"person_id": "nobody"})
    assert r.status_code == 400


def test_declaring_a_thread_with_a_contact_through_the_api(client):
    body = client.post("/threads", json={"title": "chase the plumber", "contact_person_id": "maya"}).json()
    assert body["contact_person_id"] == "maya"
    assert body["contact_name"] == "Maya"
