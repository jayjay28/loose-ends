"""The doctor on a schedule: speaks on change, stays quiet otherwise.

The failure this guards against is not "the check is wrong" — `test_doctor.py`
covers that. It is the two ways a working check still tells you nothing: it
never runs, or it runs so often you stop reading it.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from lifeline import db
from lifeline.doctor import Check, Report, FAIL, OK, WARN
from lifeline.jobs import health

from conftest import NOW


def _report(*failing: str, warning: tuple = ()) -> Report:
    report = Report()
    report.checks.append(Check("database", OK, "fine"))
    for name in failing:
        report.checks.append(Check(name, FAIL, f"{name} is broken", f"fix {name}"))
    for name in warning:
        report.checks.append(Check(name, WARN, f"{name} looks stale"))
    return report


@pytest.fixture
def doctor_says(monkeypatch):
    """Swap the doctor for a scripted answer, so these tests are about the
    alerting rules rather than about anyone's API keys."""
    box = {"report": _report()}

    def fake_run():
        return box["report"]

    from lifeline import doctor
    monkeypatch.setattr(doctor, "run", fake_run)
    return box


def _queued():
    return [r for r in db.unsent_notifications() if r["kind"] == health.KIND]


# ------------------------------------------------------------------- quiet
def test_healthy_system_says_nothing(doctor_says):
    result = health.run(NOW, force=True)
    assert result["failing"] == []
    assert result["notified"] is None
    assert _queued() == []


def test_warnings_alone_do_not_notify(doctor_says):
    """A warning is "worth a look", and a push is not a look — it is an
    interruption. Only a failure earns one."""
    doctor_says["report"] = _report(warning=("imessage freshness",))
    result = health.run(NOW, force=True)
    assert result["warning"] == ["imessage freshness"]
    assert result["notified"] is None
    assert _queued() == []


# ----------------------------------------------------------------- speaking
def test_a_new_failure_notifies_once(doctor_says):
    doctor_says["report"] = _report("imessage")
    assert health.run(NOW, force=True)["notified"] == "failing"

    queued = _queued()
    assert len(queued) == 1
    assert "imessage" in queued[0]["title"]
    # The fix travels with it: the notification arrives on a phone, nowhere
    # near the terminal that would otherwise have to be asked.
    assert "fix imessage" in queued[0]["body"]


def test_the_same_failure_does_not_notify_again(doctor_says):
    """The thing that makes an alert ignorable. iMessage was broken for 124
    consecutive cycles; 124 identical pushes would have been swiped away by
    the third."""
    doctor_says["report"] = _report("imessage")
    health.run(NOW, force=True)
    health.run(NOW + timedelta(hours=2), force=True)
    health.run(NOW + timedelta(hours=6), force=True)
    assert len(_queued()) == 1


def test_still_broken_a_day_later_says_so_again(doctor_says):
    doctor_says["report"] = _report("imessage")
    health.run(NOW, force=True)
    health.run(NOW + timedelta(hours=25), force=True)
    assert len(_queued()) == 2


def test_a_second_failure_notifies_and_carries_the_first(doctor_says):
    """A new break arriving while an old one is unfixed must not hide it —
    the message is the whole picture, not the delta."""
    doctor_says["report"] = _report("imessage")
    health.run(NOW, force=True)

    doctor_says["report"] = _report("anthropic", "imessage")
    health.run(NOW + timedelta(hours=2), force=True)

    latest = _queued()[-1]
    assert "2 checks failing" in latest["title"]
    assert "anthropic" in latest["body"] and "imessage" in latest["body"]


# ---------------------------------------------------------------- recovery
def test_recovery_is_announced(doctor_says):
    doctor_says["report"] = _report("imessage")
    health.run(NOW, force=True)

    doctor_says["report"] = _report()
    assert health.run(NOW + timedelta(hours=3), force=True)["notified"] == "recovered"
    assert "working again" in _queued()[-1]["title"]


def test_recovery_is_announced_only_once(doctor_says):
    doctor_says["report"] = _report("imessage")
    health.run(NOW, force=True)
    doctor_says["report"] = _report()
    health.run(NOW + timedelta(hours=3), force=True)
    before = len(_queued())
    health.run(NOW + timedelta(hours=30), force=True)
    assert len(_queued()) == before


# ------------------------------------------------------------ cost and safety
def test_it_does_not_run_every_cycle(doctor_says, monkeypatch):
    """The doctor refreshes a token, opens chat.db and asks two models to
    generate. Hourly that is nothing; every thirty minutes it is waste for an
    answer that does not move that fast."""
    calls = {"n": 0}
    from lifeline import doctor

    def counted():
        calls["n"] += 1
        return _report()

    monkeypatch.setattr(doctor, "run", counted)
    health.run(NOW)
    health.run(NOW + timedelta(minutes=30))
    assert calls["n"] == 1

    health.run(NOW + timedelta(minutes=90))
    assert calls["n"] == 2


def test_a_broken_doctor_cannot_break_the_poll(monkeypatch):
    """A poll cycle dying inside its own health check would be the funniest
    possible version of this bug."""
    from lifeline import doctor

    def explode():
        raise RuntimeError("the check itself is broken")

    monkeypatch.setattr(doctor, "run", explode)
    result = health.run_safely(NOW)
    assert result["ran"] is False
    assert "broken" in result["error"]
