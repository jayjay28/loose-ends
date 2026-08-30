"""v2.4 — the correction flow. One tap used to mean three things."""
from lifeline import db, threads
from lifeline.models import Finding, FindingKind, MoveKind, ThreadState
from lifeline.ranking import learning
from fastapi.testclient import TestClient
from lifeline.api.app import app

client = TestClient(app)


def _thread_with_move(title, move_kind=MoveKind.DECIDE):
    t = threads.create(title=title)
    f = Finding(thread_id=t.id, kind=FindingKind.ACTION, move_kind=move_kind,
                headline="a staged move", steps=[{"text": "do the thing"}])
    db.save_finding(f)
    return t, f


def test_wrong_records_a_correction_and_spares_the_appetite():
    """The pajamas case: `decide` was the right shape and under-decisive.
    Punishing the shape would teach the opposite of what the user meant."""
    t, f = _thread_with_move("pick pyjamas")
    before = learning.move_appetite(t, MoveKind.DECIDE)

    r = client.post(f"/threads/{t.id}/findings/{f.id}/reject",
                    json={"reason": "wrong",
                          "text": "It's for me to pick — don't ask her for size."})
    assert r.status_code == 200, r.text
    assert r.json()["correction_id"]

    assert learning.move_appetite(t, MoveKind.DECIDE) == before, "explaining cost the shape"
    said = [c.statement for c in threads.corrections(t.id)]
    assert said == ["It's for me to pick — don't ask her for size."]


def test_wrong_needs_words():
    t, f = _thread_with_move("say why")
    r = client.post(f"/threads/{t.id}/findings/{f.id}/reject",
                    json={"reason": "wrong", "text": "   "})
    assert r.status_code == 400


def test_handled_teaches_nothing_and_closes():
    """The hoop case: a move rejected for a stale reason pushed move:gather to
    0.45 against a 0.40 floor. The work was right; the timing wasn't."""
    t, f = _thread_with_move("already did it", MoveKind.GATHER)
    before = learning.move_appetite(t, MoveKind.GATHER)

    r = client.post(f"/threads/{t.id}/findings/{f.id}/reject", json={"reason": "handled"})
    assert r.status_code == 200, r.text

    assert learning.move_appetite(t, MoveKind.GATHER) == before, "a stale move cost a capability"
    assert db.get_thread(t.id).state == ThreadState.RESOLVED


def test_unwanted_still_narrows():
    """The original behaviour, kept — now a deliberate choice, not the only door."""
    t, f = _thread_with_move("stop offering this")
    before = learning.move_appetite(t, MoveKind.DECIDE)

    client.post(f"/threads/{t.id}/findings/{f.id}/reject", json={"reason": "unwanted"})
    assert learning.move_appetite(t, MoveKind.DECIDE) < before


def test_a_bare_reject_is_still_unwanted():
    """Old clients send no body at all and must keep working."""
    t, f = _thread_with_move("legacy client")
    before = learning.move_appetite(t, MoveKind.DECIDE)
    r = client.post(f"/threads/{t.id}/findings/{f.id}/reject")
    assert r.status_code == 200, r.text
    assert learning.move_appetite(t, MoveKind.DECIDE) < before


def test_corrections_reach_the_worker():
    """A correction that the brief doesn't carry is theatre."""
    t = threads.create(title="tell it something")
    threads.correct(t.id, "The choosing is mine here.")
    assert threads.draft_brief(t.id)["what_you_told_me"] == ["The choosing is mine here."]


def test_a_correction_can_be_taken_back():
    t = threads.create(title="take it back")
    c = client.post(f"/threads/{t.id}/corrections", json={"statement": "wrong about this"}).json()

    detail = client.get(f"/threads/{t.id}").json()
    assert [x["statement"] for x in detail["corrections"]] == ["wrong about this"]

    assert client.delete(f"/threads/{t.id}/corrections/{c['id']}").status_code == 200
    assert client.get(f"/threads/{t.id}").json()["corrections"] == []
    assert threads.draft_brief(t.id)["what_you_told_me"] == []


def test_a_correction_needs_words():
    t = threads.create(title="empty")
    assert client.post(f"/threads/{t.id}/corrections", json={"statement": "  "}).status_code == 400


def test_corrections_are_scoped_to_their_thread():
    a = threads.create(title="thread a")
    b = threads.create(title="thread b")
    ca = client.post(f"/threads/{a.id}/corrections", json={"statement": "for a"}).json()

    assert client.delete(f"/threads/{b.id}/corrections/{ca['id']}").status_code == 404
    assert threads.draft_brief(b.id)["what_you_told_me"] == []
