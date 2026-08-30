"""§v2.1 — a move is an `action` finding with structure.

Not a new table. `action` already means "something prepared for you", already
renders, already carries provenance, and already flows through the interruption
budget. What it lacked was a way to say what *kind* of move it is, what was
actually staged, and what only the user can supply.

The validation here is the spec's decision 1 made mechanical: a move that
cannot be acted on without thinking is a finding, and the tool refuses to let
the worker file a note-to-itself as prepared work.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lifeline import db, threads
from lifeline.api.app import app
from lifeline.assistant import registry
from lifeline.models import Autonomy, FindingKind, MoveKind

from tests.conftest import make_conversation, make_person


@pytest.fixture(autouse=True)
def default_person():
    make_person()
    make_conversation()


def _tool(thread_id):
    """Built the way `scoped_for` builds it — the thread's own ceiling decides
    which move shapes it will accept."""
    thread = db.get_thread(thread_id)
    return registry.by_name(
        registry.finding_tools(thread_id, ceiling=thread.autonomy)
    )["record_finding"]


def _at(ceiling, **kw):
    thread = threads.create(**kw)
    threads.set_autonomy(thread.id, ceiling)
    return thread


# --------------------------------------------------------- the shape
def test_a_move_carries_its_kind_and_staged_work():
    thread = _at(Autonomy.ASK, title="Pay the American Water bill")
    _tool(thread.id).fn(
        headline="Pay $153.52 now + $231.88 by Aug 31 = $385.40",
        kind=FindingKind.ACTION,
        move_kind=MoveKind.DO,
        steps=["$153.52 past due — pay immediately",
               "$231.88 current — due Aug 31",
               "https://secure7.striata.com/pay"],
        needs=["a payment method"],
    )
    f = db.thread_findings(thread.id)[0]
    assert f.move_kind == MoveKind.DO
    assert len(f.steps) == 3
    assert f.needs == ["a payment method"]
    assert f.blocked_reason is None


def test_an_ordinary_finding_carries_none_of_it():
    thread = threads.create(title="a loop")
    _tool(thread.id).fn(headline="something happened")
    f = db.thread_findings(thread.id)[0]
    assert f.move_kind is None and f.steps == [] and f.needs == []


# ------------------------------------------------- the guardrails
def test_a_move_must_say_which_shape_it_is():
    """Without a shape the client can't render it as a move, and the four
    shapes want different affordances — a draft to send is not a bill to pay."""
    thread = threads.create(title="a loop")
    r = _tool(thread.id).fn(headline="do the thing", kind=FindingKind.ACTION)
    assert "move_kind" in r["error"]
    assert db.thread_findings(thread.id) == []


def test_a_move_with_no_staged_work_and_no_reason_is_refused():
    """The failure the prompt spends a paragraph on: an "action" whose body is
    instructions for assembling an answer. Describing the work is not doing it,
    so the tool will not accept it as prepared."""
    thread = threads.create(title="a loop")
    r = _tool(thread.id).fn(
        headline="Build a study plan", kind=FindingKind.ACTION, move_kind=MoveKind.GATHER,
    )
    assert "steps" in r["error"] and "blocked_reason" in r["error"]
    assert db.thread_findings(thread.id) == []


def test_naming_a_move_you_could_not_stage_is_allowed():
    """Decision 2: "I know what would close this and could not do it" is a real
    answer, and has to look different from a move that's ready."""
    thread = _at(Autonomy.ASK, title="a loop")
    _tool(thread.id).fn(
        headline="Someone has to call the Land Bank",
        kind=FindingKind.ACTION,
        move_kind=MoveKind.DO,
        blocked_reason="No number in any thread evidence, and I cannot search the web.",
    )
    f = db.thread_findings(thread.id)[0]
    assert f.steps == []
    assert "cannot search the web" in f.blocked_reason


def test_an_invented_shape_is_refused():
    thread = threads.create(title="a loop")
    r = _tool(thread.id).fn(
        headline="x", kind=FindingKind.ACTION, move_kind="teleport", steps=["a"],
    )
    assert "move_kind" in r["error"]


def test_move_fields_do_not_belong_on_a_plain_finding():
    """A finding with steps is a move that forgot to say so — silently
    accepting it would mean the UI never renders it as one."""
    thread = threads.create(title="a loop")
    r = _tool(thread.id).fn(headline="x", kind=FindingKind.FINDING, steps=["a"])
    assert "error" in r


# ------------------------------------------------------------ the wire
def test_a_move_reaches_the_client_whole():
    thread = threads.create(title="a loop")
    _tool(thread.id).fn(
        headline="Send Guest Relations your arrival time",
        kind=FindingKind.ACTION,
        move_kind=MoveKind.SEND,
        steps=["Draft: 'I land at 12:04 and should reach the hotel by 12:30.'"],
        needs=["your OK to send"],
    )
    body = TestClient(app).get(f"/threads/{thread.id}").json()
    f = body["findings"][0]
    assert f["move_kind"] == "send"
    # Flattened to strings at the boundary — stored as dicts so a step can grow
    # fields later without a migration.
    assert f["steps"] == ["Draft: 'I land at 12:04 and should reach the hotel by 12:30.'"]
    assert f["needs"] == ["your OK to send"]


def test_a_blocked_move_reaches_the_client_as_blocked():
    thread = _at(Autonomy.ASK, title="a loop")
    _tool(thread.id).fn(
        headline="Someone has to call them", kind=FindingKind.ACTION,
        move_kind=MoveKind.DO, blocked_reason="no number anywhere in the thread",
    )
    f = TestClient(app).get(f"/threads/{thread.id}").json()["findings"][0]
    assert f["blocked_reason"] == "no number anywhere in the thread"
    assert f["steps"] == []


# ------------------------------------------------- the ladder (step 3)
def test_prepared_may_propose_everything_reversible():
    """Send, decide and gather cost nothing and undo themselves. They need no
    permission, which is what `prepared` has always meant."""
    for shape in (MoveKind.SEND, MoveKind.DECIDE, MoveKind.GATHER):
        thread = threads.create(title=f"a {shape} loop")
        r = _tool(thread.id).fn(
            headline=f"a {shape} move", kind=FindingKind.ACTION,
            move_kind=shape, steps=["the staged work"],
        )
        assert "error" not in r, r


def test_prepared_may_not_propose_a_do_move():
    """`do` is the shape that reaches outside the app — pay, upload, buy. That
    is what the third rung was reserved for, and until v2.1 nothing used it."""
    thread = threads.create(title="Pay the water bill")
    r = _tool(thread.id).fn(
        headline="Pay $385.40", kind=FindingKind.ACTION,
        move_kind=MoveKind.DO, steps=["https://pay.example/x"],
    )
    assert "error" in r
    assert "ask" in r["error"]
    assert db.thread_findings(thread.id) == []


def test_ask_may_propose_a_do_move():
    thread = _at(Autonomy.ASK, title="Pay the water bill")
    r = _tool(thread.id).fn(
        headline="Pay $385.40", kind=FindingKind.ACTION,
        move_kind=MoveKind.DO, steps=["https://pay.example/x"],
    )
    assert "error" not in r, r
    assert db.thread_findings(thread.id)[0].move_kind == MoveKind.DO


def test_silent_takes_no_moves_at_all():
    """At this rung the worker may say what is missing and never stage the
    thing that fills it. That is the whole content of the rung, and it is what
    the iOS copy promises."""
    thread = _at(Autonomy.SILENT, title="a loop")
    r = _tool(thread.id).fn(
        headline="Send the reply", kind=FindingKind.ACTION,
        move_kind=MoveKind.SEND, steps=["a draft"],
    )
    assert "error" in r
    assert "no moves at all" in r["error"]


def test_silent_still_records_ordinary_findings():
    """Gated on proposing, not on thinking. A silent thread is still worked."""
    thread = _at(Autonomy.SILENT, title="a loop")
    r = _tool(thread.id).fn(headline="the bill is due Friday")
    assert "error" not in r
    assert db.thread_findings(thread.id)[0].headline == "the bill is due Friday"


def test_the_refusal_tells_the_worker_what_to_do_instead():
    """A bare rejection wastes the pass — the worker has already done the
    thinking, so the error routes it to the finding it should file instead."""
    thread = threads.create(title="a loop")
    r = _tool(thread.id).fn(
        headline="Pay it", kind=FindingKind.ACTION,
        move_kind=MoveKind.DO, steps=["link"],
    )
    assert "kind='finding'" in r["error"]


def test_the_ladder_matches_scoped_for():
    """`scoped_for` is what the worker actually gets, so the gate has to be
    reachable through it and not only through a hand-built tool list."""
    thread = threads.create(title="a loop")
    tool = registry.by_name(registry.scoped_for(db.get_thread(thread.id)))["record_finding"]
    assert "error" in tool.fn(
        headline="Pay it", kind=FindingKind.ACTION,
        move_kind=MoveKind.DO, steps=["link"],
    )


# ------------------------------- rejection as a signal (step 5)
def test_rejecting_a_move_narrows_appetite_for_that_shape():
    """What a rejection teaches, which the spec left open. Not "this thread
    wants nothing" — too broad for one tap. Not "wrong moment" — that is what
    quiet is for, and `record_thread` already refuses to learn from it."""
    from lifeline.ranking import learning

    thread = _at(Autonomy.ASK, title="Pay the bill")
    before = learning.move_appetite(thread, MoveKind.DO)
    learning.record_move("rejected", thread, MoveKind.DO)
    assert learning.move_appetite(thread, MoveKind.DO) < before


def test_rejecting_one_shape_leaves_the_others_alone():
    """Turning down a payment link should not stop the system drafting."""
    from lifeline.ranking import learning

    thread = _at(Autonomy.ASK, title="a loop")
    before_send = learning.move_appetite(thread, MoveKind.SEND)
    learning.record_move("rejected", thread, MoveKind.DO)
    assert learning.move_appetite(thread, MoveKind.SEND) == before_send


def test_enough_rejections_stop_the_shape_being_offered():
    from lifeline.ranking import learning

    thread = _at(Autonomy.ASK, title="a loop")
    assert learning.may_propose(thread, MoveKind.DO) is True
    for _ in range(4):
        learning.record_move("rejected", thread, MoveKind.DO)
    assert learning.may_propose(thread, MoveKind.DO) is False


def test_one_rejection_is_not_enough_to_silence_a_shape():
    """A single no narrows the odds. Silencing on one tap would make the
    system brittle in the direction of doing nothing."""
    from lifeline.ranking import learning

    thread = _at(Autonomy.ASK, title="a loop")
    learning.record_move("rejected", thread, MoveKind.DO)
    assert learning.may_propose(thread, MoveKind.DO) is True


def test_acceptance_restores_appetite_so_it_is_not_a_ratchet():
    """A signal that only ever falls would let initiative decay to nothing as
    rejections accumulated, with nobody deciding that it should."""
    from lifeline.ranking import learning

    thread = _at(Autonomy.ASK, title="a loop")
    for _ in range(4):
        learning.record_move("rejected", thread, MoveKind.DO)
    silenced = learning.move_appetite(thread, MoveKind.DO)
    for _ in range(4):
        learning.record_move("accepted", thread, MoveKind.DO)
    assert learning.move_appetite(thread, MoveKind.DO) > silenced


def test_a_silenced_shape_is_refused_at_the_tool():
    """Appetite has to bite where the ceiling does, or it is only advice."""
    from lifeline.ranking import learning

    thread = _at(Autonomy.ASK, title="a loop")
    for _ in range(4):
        learning.record_move("rejected", thread, MoveKind.DO)
    r = _tool(thread.id).fn(
        headline="Pay it", kind=FindingKind.ACTION,
        move_kind=MoveKind.DO, steps=["a link"],
    )
    assert "error" in r
    assert "kind='finding'" in r["error"]


def test_learning_may_narrow_and_never_widen():
    """The ladder's asymmetry, extended. Accepting `do` moves on a `prepared`
    thread must never unlock a rung the user did not grant."""
    from lifeline.ranking import learning

    thread = threads.create(title="a loop")          # prepared
    for _ in range(6):
        learning.record_move("accepted", thread, MoveKind.DO)
    r = _tool(thread.id).fn(
        headline="Pay it", kind=FindingKind.ACTION,
        move_kind=MoveKind.DO, steps=["a link"],
    )
    assert "error" in r and "ask" in r["error"]


def test_the_verdicts_round_trip_through_the_api():
    from lifeline.ranking import learning

    client = TestClient(app)
    thread = _at(Autonomy.ASK, title="a loop")
    _tool(thread.id).fn(
        headline="Pay it", kind=FindingKind.ACTION,
        move_kind=MoveKind.DO, steps=["a link"],
    )
    fid = db.thread_findings(thread.id)[0].id

    body = client.post(f"/threads/{thread.id}/findings/{fid}/reject").json()
    assert body["rejected"] == fid
    assert body["appetite"] < learning.MOVE_PRIOR
    # Dismissed, not deleted — a rejected move is evidence about the user.
    assert db.get_finding(fid).dismissed_at is not None
    assert db.thread_findings(thread.id) == []


def test_accepting_through_the_api_teaches_the_other_way():
    from lifeline.ranking import learning

    client = TestClient(app)
    thread = _at(Autonomy.ASK, title="a loop")
    _tool(thread.id).fn(
        headline="Pay it", kind=FindingKind.ACTION,
        move_kind=MoveKind.DO, steps=["a link"],
    )
    fid = db.thread_findings(thread.id)[0].id
    body = client.post(f"/threads/{thread.id}/findings/{fid}/accept").json()
    assert body["appetite"] > learning.MOVE_PRIOR
    # Accepting keeps it on the thread — the user may still want to act again.
    assert db.get_finding(fid).dismissed_at is None


def test_leaked_call_markup_is_cut_and_its_facts_salvaged():
    """Seen live on the Netflix thread: the model stuffed its own tool-call
    syntax inside record_finding's body string — '…reviews all
    submissions.</body> <parameter name="facts">[{…}]' rendered verbatim on
    the phone, while the facts column stored []. The markup is machinery,
    cut at the point of record; the facts it was carrying are salvaged."""
    from lifeline.assistant.registry import _plain, salvage_call_markup

    leaked = (
        'The studio actively reviews all submissions.</body> '
        '<parameter name="facts">[{"label":"Next Games Contact",'
        '"value":"info@nextgames.com","url":"https://nextgames.com/contact"}]'
    )
    assert _plain(leaked) == "The studio actively reviews all submissions."
    facts = salvage_call_markup(leaked)
    assert facts[0]["label"] == "Next Games Contact"

    # A tail with no facts payload just gets cut.
    assert _plain("Fine answer.<parameter name=\"importance\">") == "Fine answer."
    assert salvage_call_markup("Fine answer.<parameter name=\"importance\">") == []
