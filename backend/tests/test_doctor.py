"""§v2.4 — the doctor.

Every check here has to *attempt* the thing rather than inspect configuration,
because configuration was correct throughout all three of the silent failures
that motivated it: a dead `record_finding`, thirteen days of dead Gmail behind
`google_connected: true`, and iMessage returning 0 for want of Full Disk Access.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lifeline import db, doctor


def test_the_database_check_reports_what_it_found():
    check = doctor.check_database()
    assert check.status == doctor.OK
    assert "threads" in check.detail


def test_a_check_that_raises_never_stops_the_rest():
    """A doctor that dies on the first problem reports less than no doctor."""
    def explode():
        raise RuntimeError("boom")

    with patch.object(doctor, "CHECKS", [explode, doctor.check_database]):
        report = doctor.run()

    assert len(report.checks) == 2
    assert report.checks[0].status == doctor.FAIL
    assert "boom" in report.checks[0].detail
    assert report.checks[1].status == doctor.OK


def test_a_missing_key_is_skipped_not_failed():
    """Not configured is a different answer from broken, and the setup flow
    needs to tell them apart."""
    from lifeline import config

    with patch.object(config, "get_config", lambda: config.Config(anthropic_api_key="")):
        with patch.object(doctor, "get_config", lambda: config.Config(anthropic_api_key="")):
            assert doctor.check_anthropic().status == doctor.SKIP


def test_a_rejected_key_fails_rather_than_passing_on_presence():
    """The whole point: a key that exists but does not work."""
    from lifeline import config

    cfg = config.Config(anthropic_api_key="sk-ant-dead")
    with patch.object(doctor, "get_config", lambda: cfg):
        with patch("lifeline.extraction.claude._client") as client:
            client.return_value.messages.count_tokens.side_effect = RuntimeError(
                "credit balance is too low"
            )
            check = doctor.check_anthropic()

    assert check.status == doctor.FAIL
    assert "credit balance" in check.detail


def test_stale_ingestion_warns_even_though_the_read_works():
    """Gmail's token died while every structural check would have passed. The
    only tell was that nothing new had arrived for thirteen days."""
    from conftest import make_conversation
    from lifeline.models import Message, new_id

    make_conversation("gmail:old", source="gmail", name="Someone")
    old = (datetime.now(timezone.utc) - timedelta(days=13)).isoformat(timespec="seconds")
    # Built directly: `make_message` hardcodes source="imessage", and the
    # source is the whole point of this check.
    db.insert_messages([Message(
        id=new_id(), source="gmail", conversation_id="gmail:old",
        external_id=new_id(), person_id=None, is_from_user=False,
        timestamp=old, text="an old email", metadata={},
    )])

    freshness = {c.name: c for c in doctor.check_freshness()}
    gmail = freshness["gmail freshness"]
    assert gmail.status == doctor.WARN
    assert "stalled" in gmail.detail


def test_a_pass_whose_every_recording_was_refused_is_reported():
    """The dead-guard signature: it tried to write down what it found, and was
    refused every time it tried."""
    calls = json.dumps([
        {"name": "read_thread_state", "result": "{}"},
        {"name": "record_finding", "result": "{'error': 'this names prices but has no facts'}"},
        {"name": "record_finding", "result": "{'error': 'this names prices but has no facts'}"},
    ])
    db.get_connection().execute(
        "INSERT INTO loop_runs (id, trigger, goal, provider, status, iterations, "
        "tool_calls, conclusion, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("run-lost", "worker", "work it", "claude", "ok", 3, calls, "",
         datetime.now(timezone.utc).isoformat()),
    )
    db.get_connection().commit()

    check = doctor.check_worker_recording()
    assert check.status in (doctor.FAIL, doctor.WARN)
    assert "recorded nothing" in check.detail


def test_a_refusal_among_successes_is_not_an_alarm():
    """The guards refuse bad findings on purpose. One refusal is the system
    working, not failing."""
    mixed = json.dumps([
        {"name": "record_finding", "result": "{'error': 'a decide that says this or that has not decided'}"},
        {"name": "record_finding", "result": "{'recorded': 'abc'}"},
    ])
    db.get_connection().execute(
        "INSERT INTO loop_runs (id, trigger, goal, provider, status, iterations, "
        "tool_calls, conclusion, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("run-ok", "worker", "work it", "claude", "ok", 2, mixed, "",
         datetime.now(timezone.utc).isoformat()),
    )
    db.get_connection().commit()

    assert doctor.check_worker_recording().status == doctor.OK


def test_caching_silently_off_is_caught():
    """Counted from day one, never enabled, and nothing reported it."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db.set_sync_state(f"llm_tokens_in:{day}", "400000")
    db.set_sync_state(f"llm_tokens_cached:{day}", "0")

    check = doctor.check_caching()
    assert check.status == doctor.WARN
    assert "none served from cache" in check.detail


def test_the_report_serialises_for_the_setup_flow():
    report = doctor.run()
    payload = report.as_dict()
    assert set(payload) == {"healthy", "checks"}
    assert all(set(c) == {"name", "status", "detail", "fix"} for c in payload["checks"])


# ------------------------------------------------------- the fallback provider

class _GeminiResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


def test_a_depleted_gemini_key_fails_rather_than_reporting_configured():
    """`countTokens` is unmetered and answered OK on this key at the same
    moment `generateContent` returned 429 — so the check has to generate."""
    import httpx
    from lifeline import config

    cfg = config.Config(gemini_api_key="AIza-dead")
    body = '{\n  "error": {\n    "code": 429,\n    "message": "Your prepayment credits are depleted."\n  }\n}'
    with patch.object(doctor, "get_config", lambda: cfg):
        with patch.object(httpx, "post", return_value=_GeminiResponse(429, body)) as post:
            check = doctor.check_gemini()

    assert check.status == doctor.FAIL
    assert "depleted" in check.detail
    assert "\n" not in check.detail          # one line, whatever Google sends
    assert post.call_args.args[0].endswith(":generateContent")


def test_a_working_gemini_key_passes():
    import httpx
    from lifeline import config

    cfg = config.Config(gemini_api_key="AIza-live")
    with patch.object(doctor, "get_config", lambda: cfg):
        with patch.object(httpx, "post", return_value=_GeminiResponse(200)):
            check = doctor.check_gemini()

    assert check.status == doctor.OK


def test_no_gemini_key_is_a_skip_not_a_failure():
    """It is the fallback, not a requirement."""
    from lifeline import config

    cfg = config.Config(gemini_api_key="")
    with patch.object(doctor, "get_config", lambda: cfg):
        assert doctor.check_gemini().status == doctor.SKIP


def test_unread_attachments_are_counted_not_invisible():
    """125 attachment-bearing messages, zero ingested, every check green —
    because reading a message's body counted as reading the message."""
    from tests.conftest import make_conversation, make_message
    make_conversation("gmail:t1", source="gmail", name="school")
    make_message("see attached", conversation_id="gmail:t1", person_id=None,
                 metadata={"attachments": [{"filename": "form.pdf"}]})
    make_message("no files here", conversation_id="gmail:t1", person_id=None)

    check = doctor.check_attachments()
    assert check.status == doctor.WARN
    assert "1 of 1" in check.detail


def test_no_attachment_metadata_yet_is_a_skip_with_directions():
    check = doctor.check_attachments()
    assert check.status == doctor.SKIP
    assert "attachments scan" in check.fix
