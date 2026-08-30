"""§v2 step 4 — the worker loop: the system working while nobody asked.

The three constraints under test are decisions, not implementation details:
cadence (what makes a thread due), budget (the worker cannot starve extraction
and cannot be starved by it), and honesty ("I looked and found nothing" is
recorded, not discarded).
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from lifeline import db, threads
from lifeline.assistant import loop as assistant_loop, registry, worker
from lifeline.extraction import budget, providers
from lifeline.models import Autonomy, Evidence, FindingKind, ThreadState

from tests.conftest import make_conversation, make_item, make_person

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def default_person():
    return make_person()


def _provider(script):
    turns = list(script)

    def complete_with_tools(messages, *, tools, system=None, max_tokens=1024):
        return turns.pop(0) if turns else {"text": "out of script", "tool_calls": []}

    return types.SimpleNamespace(__name__="fake", complete_with_tools=complete_with_tools)


def _records(headline, kind="finding"):
    """A provider that records one finding and stops."""
    return _provider([
        {"text": "", "tool_calls": [{
            "id": "c1", "name": "record_finding",
            "input": {"headline": headline, "body": "because of X", "kind": kind},
        }]},
        {"text": "Recorded one finding.", "tool_calls": []},
    ])


# ------------------------------------------------------------- the cadence
def test_a_thread_never_worked_is_due():
    thread = threads.create(title="Puerto Rico work trip")
    assert [t.id for t in worker.due(NOW)] == [thread.id]


def test_a_thread_worked_just_now_is_not_due():
    thread = threads.create(title="Puerto Rico work trip")
    thread.last_worked_at = NOW.isoformat(timespec="seconds")
    db.save_thread(thread)
    assert worker.due(NOW) == []


def test_new_evidence_makes_a_thread_due_again():
    """The first cadence trigger: something actually arrived for it."""
    item = make_item()
    thread = threads.create(title="a loop")
    thread.last_worked_at = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
    db.save_thread(thread)
    assert worker.due(NOW) == []

    db.add_evidence(Evidence(
        thread_id=thread.id, kind="item", ref_id=item.id,
        linked_at=(NOW - timedelta(minutes=5)).isoformat(timespec="seconds"),
    ))
    assert [t.id for t in worker.due(NOW)] == [thread.id]


def test_a_stale_thread_comes_due_without_any_evidence():
    """The second trigger: nothing arrived, but time passed. Without this a
    thread with a deadline and a quiet inbox is never revisited — nothing
    would notice the date approaching."""
    thread = threads.create(title="a loop with a deadline")
    thread.last_worked_at = (NOW - timedelta(hours=worker.DAILY_FLOOR_HOURS + 1)).isoformat(timespec="seconds")
    db.save_thread(thread)
    assert [t.id for t in worker.due(NOW)] == [thread.id]


def test_quiet_threads_are_still_worked_but_sort_last():
    """Quiet means "stop surfacing", not "stop thinking"."""
    quiet = threads.create(title="hushed", state=ThreadState.QUIET)
    live = threads.create(title="running")
    order = [t.id for t in worker.due(NOW)]
    assert set(order) == {quiet.id, live.id}
    assert order[-1] == quiet.id


def test_resolved_threads_are_never_worked():
    thread = threads.create(title="done")
    threads.resolve(thread.id)
    assert worker.due(NOW) == []


def test_a_cycle_is_capped_so_a_backlog_drains_gradually(monkeypatch):
    for n in range(6):
        threads.create(title=f"thread {n}")
    monkeypatch.setattr(providers, "available", lambda: [_records("something")])
    result = worker.run(NOW)
    assert result["worked"] == worker.MAX_THREADS_PER_CYCLE


# -------------------------------------------------------------- the budget
def test_the_worker_has_its_own_ceiling(monkeypatch):
    """The audit's warning: one shared counter lets a heavy ingest day starve
    autonomous work, and a runaway worker starve extraction. Neither failure
    announces itself."""
    monkeypatch.setenv("LIFELINE_BUDGET_WORKER", "0")   # 0 = no own ceiling
    assert budget.allow("worker") is True

    monkeypatch.setenv("LIFELINE_BUDGET_WORKER", "2")
    budget.record("worker")
    budget.record("worker")
    assert budget.allow("worker") is False
    # Extraction is untouched by the worker's spend.
    assert budget.allow("classify") is True


def test_a_spent_worker_budget_stops_the_cycle_without_touching_threads(monkeypatch):
    monkeypatch.setenv("LIFELINE_BUDGET_WORKER", "1")
    budget.record("worker")
    thread = threads.create(title="a loop")
    assert worker.run(NOW)["worked"] == 0
    assert db.get_thread(thread.id).last_worked_at is None


def test_the_global_cap_still_applies(monkeypatch):
    monkeypatch.setenv("LIFELINE_LLM_DAILY_CALL_CAP", "1")
    budget.record()
    assert budget.allow("worker") is False


def test_spend_is_reportable():
    budget.record("worker")
    report = budget.spend_report()
    assert report["worker"]["used"] == 1
    assert report["worker"]["cap"] == budget.TRIGGER_CAPS["worker"]


# ------------------------------------------------------------- the honesty
def test_a_pass_that_finds_nothing_still_records_that(monkeypatch):
    """"I looked and found nothing" is a real result. It's what the grey marks
    on the activity track mean, and a system that logs only its hits is
    misrepresenting its work."""
    thread = threads.create(title="a quiet loop")
    monkeypatch.setattr(providers, "available", lambda: [_provider([
        {"text": "Nothing new here.", "tool_calls": []},
    ])])
    worker.work(thread.id, NOW)

    findings = db.thread_findings(thread.id)
    assert len(findings) == 1
    assert findings[0].kind == FindingKind.NOTHING


def test_a_recorded_finding_carries_its_provenance(monkeypatch):
    """A finding whose run can't be opened is an assertion."""
    thread = threads.create(title="a loop")
    monkeypatch.setattr(providers, "available", lambda: [_records("The flight moved to 6:10pm")])
    run = worker.work(thread.id, NOW)

    finding = db.thread_findings(thread.id)[0]
    assert finding.headline == "The flight moved to 6:10pm"
    assert finding.loop_run_id == run.run_id
    row = db.get_connection().execute(
        "SELECT trigger FROM loop_runs WHERE id = ?", (run.run_id,)
    ).fetchone()
    assert row["trigger"] == "worker"


def test_the_same_finding_is_not_recorded_twice(monkeypatch):
    """The worker runs on a schedule, so without this the thread repeats
    itself every pass — noise wearing the costume of diligence."""
    thread = threads.create(title="a loop")
    monkeypatch.setattr(providers, "available", lambda: [_records("The flight moved")])
    worker.work(thread.id, NOW)

    monkeypatch.setattr(providers, "available", lambda: [_records("The flight moved")])
    worker.work(thread.id, NOW + timedelta(days=1))

    headlines = [f.headline for f in db.thread_findings(thread.id)]
    assert headlines.count("The flight moved") == 1


def test_working_a_thread_marks_it_worked(monkeypatch):
    thread = threads.create(title="a loop")
    monkeypatch.setattr(providers, "available", lambda: [_records("something")])
    worker.work(thread.id, NOW)
    assert db.get_thread(thread.id).last_worked_at is not None
    assert worker.due(NOW) == []


def test_findings_appear_on_the_activity_track(monkeypatch):
    """The track was built in step 2 to carry these without changing shape."""
    thread = threads.create(title="a loop")
    monkeypatch.setattr(providers, "available", lambda: [_records("found it", kind="finding")])
    worker.work(thread.id, NOW)
    assert "finding" in {m["kind"] for m in threads.activity(thread.id)}


# ------------------------------------------------- per-thread tool scoping
def test_a_prepared_thread_may_draft():
    """The default ceiling. Preparing needs no permission — it's visible and
    never sent."""
    thread = threads.create(title="a loop")
    names = {t.name for t in registry.scoped_for(thread)}
    assert "draft_message" in names
    assert "record_finding" in names


def test_a_silent_thread_may_only_read():
    thread = threads.create(title="a loop")
    threads.set_autonomy(thread.id, Autonomy.SILENT)
    names = {t.name for t in registry.scoped_for(db.get_thread(thread.id))}
    assert "draft_message" not in names
    # Reading and recording are always allowed — that's what silent means.
    assert {"search_messages", "record_finding"} <= names


def test_autonomy_must_be_a_real_rung():
    thread = threads.create(title="a loop")
    with pytest.raises(threads.ThreadError):
        threads.set_autonomy(thread.id, "do-whatever")


def test_record_finding_is_bound_to_one_thread():
    """`execute(tool, args)` has no thread context, which is why the ladder had
    nowhere to live. The tool set is now a function of the thread."""
    a = threads.create(title="thread A")
    b = threads.create(title="thread B")
    tool = registry.by_name(registry.scoped_for(a))["record_finding"]
    tool.fn(headline="belongs to A")

    assert [f.headline for f in db.thread_findings(a.id)] == ["belongs to A"]
    assert db.thread_findings(b.id) == []


# --------------------------------------------------------------- the wire
def test_findings_reach_the_thread_detail(monkeypatch):
    from fastapi.testclient import TestClient

    from lifeline.api.app import app

    thread = threads.create(title="a loop")
    monkeypatch.setattr(providers, "available", lambda: [_records("The bill is due Friday")])
    worker.work(thread.id, NOW)

    body = TestClient(app).get(f"/threads/{thread.id}").json()
    assert body["findings"][0]["headline"] == "The bill is due Friday"
    assert body["findings"][0]["loop_run_id"]
    assert body["thread"]["autonomy"] == "prepared"


def test_the_poller_runs_the_worker(monkeypatch):
    from lifeline.jobs import poller

    threads.create(title="a loop")
    monkeypatch.setattr(providers, "available", lambda: [_records("found something")])
    assert poller.cycle(NOW)["worker"]["worked"] == 1


# ------------------------------------------------- the ceiling is settable
def test_the_ceiling_can_be_set_through_the_api():
    """It was enforced server-side from the start and had no way to be set —
    a ladder with no controls is just a constant."""
    from fastapi.testclient import TestClient

    from lifeline.api.app import app

    client = TestClient(app)
    thread = threads.create(title="a loop")
    assert client.get(f"/threads/{thread.id}").json()["thread"]["autonomy"] == "prepared"

    body = client.post(f"/threads/{thread.id}/autonomy", json={"ceiling": "silent"}).json()
    assert body["autonomy"] == "silent"
    # And it actually narrows what the thread may do.
    names = {t.name for t in registry.scoped_for(db.get_thread(thread.id))}
    assert "draft_message" not in names


def test_an_invented_rung_is_refused():
    from fastapi.testclient import TestClient

    from lifeline.api.app import app

    thread = threads.create(title="a loop")
    r = TestClient(app).post(f"/threads/{thread.id}/autonomy", json={"ceiling": "do-anything"})
    assert r.status_code == 400


def test_raising_a_ceiling_is_only_ever_the_users_doing():
    """Learning may lower a thread and never raise it — `prepared` already
    needs no permission, so the only rung learning could promote into is the
    irreversible one."""
    import inspect

    from lifeline.ranking import learning

    thread = threads.create(title="a loop")
    threads.set_autonomy(thread.id, Autonomy.SILENT)

    # Nothing in the learning path *writes* autonomy. Read through the module
    # object rather than a relative path — the first version of this opened
    # "lifeline/ranking/learning.py", which only resolves when pytest happens
    # to run from `backend/`.
    #
    # Checks for writes rather than for the word: v2.1 added move appetite,
    # whose whole design is the same never-raise asymmetry, and it cannot
    # explain itself without naming the ladder. Banning the substring outright
    # would forbid the comment while permitting the bug.
    source = inspect.getsource(learning)
    assert "set_autonomy" not in source
    assert ".autonomy =" not in source
    assert "autonomy=" not in source


# ------------------------------------ what today looks like (§v2.1 step 6)
def test_the_pass_is_told_what_the_next_two_days_hold():
    """§v2.1's situational awareness, and it needs no sensors. The worker
    produced the most useful sentence in the live database this way — "you're
    leaving for Puerto Rico tomorrow morning, so this may need to be handled
    before then" — by crossing a bill against a calendar entry."""
    from lifeline.models import CalendarEvent

    db.upsert_calendar_events([CalendarEvent(
        id="e1", calendar_id="primary", summary="Flight to San Juan",
        start_at=(NOW + timedelta(hours=20)).isoformat(timespec="seconds"),
        end_at=(NOW + timedelta(hours=24)).isoformat(timespec="seconds"),
    )])
    coming = worker.whats_coming(NOW)
    assert [e["what"] for e in coming] == ["Flight to San Juan"]
    assert coming[0]["status"] == "ahead"


def test_events_beyond_the_horizon_are_left_out():
    """A meeting on Thursday changes nothing about a bill due in September, and
    a longer window is more tokens for less signal."""
    from lifeline.models import CalendarEvent

    db.upsert_calendar_events([CalendarEvent(
        id="e1", calendar_id="primary", summary="Dentist next month",
        start_at=(NOW + timedelta(days=30)).isoformat(timespec="seconds"),
        end_at=(NOW + timedelta(days=30, hours=1)).isoformat(timespec="seconds"),
    )])
    assert worker.whats_coming(NOW) == []


def test_something_already_under_way_is_still_reported():
    """Dropping everything in the past would hide the case that matters most —
    the trip the user is on right now."""
    from lifeline.models import CalendarEvent

    db.upsert_calendar_events([CalendarEvent(
        id="e1", calendar_id="primary", summary="In San Juan",
        start_at=(NOW - timedelta(hours=3)).isoformat(timespec="seconds"),
        end_at=(NOW + timedelta(hours=3)).isoformat(timespec="seconds"),
    )])
    coming = worker.whats_coming(NOW)
    assert coming[0]["status"] == "under way or just finished"


def test_the_calendar_reaches_the_prompt(monkeypatch):
    """It has to be in the brief the model actually sees, not merely available
    through a tool it may never call."""
    from lifeline.models import CalendarEvent

    seen = {}

    def _capture(prompt, **kw):
        seen["prompt"] = prompt
        return None

    db.upsert_calendar_events([CalendarEvent(
        id="e1", calendar_id="primary", summary="Flight to San Juan",
        start_at=(NOW + timedelta(hours=20)).isoformat(timespec="seconds"),
        end_at=(NOW + timedelta(hours=22)).isoformat(timespec="seconds"),
    )])
    thread = threads.create(title="Pay the water bill")
    monkeypatch.setattr(assistant_loop, "run_loop", _capture)
    worker.work(thread.id, NOW)

    assert "what_today_looks_like" in seen["prompt"]
    assert "Flight to San Juan" in seen["prompt"]


def test_recurring_instances_do_not_fill_the_window():
    """The live calendar carries recurring events as separate rows with
    distinct ids — "Paper Recycling" appeared five times at the same minute.
    Without deduping, the window fills with repeats and the real day is pushed
    out of it."""
    from lifeline.models import CalendarEvent

    when = (NOW + timedelta(hours=10)).isoformat(timespec="seconds")
    db.upsert_calendar_events([
        CalendarEvent(id=f"e{n}", calendar_id="primary", summary="Paper Recycling",
                      start_at=when, end_at=when)
        for n in range(5)
    ])
    assert len(worker.whats_coming(NOW)) == 1
