"""§v2 step 3 — sharper retrieval, and the rule that makes it get used.

Every case here was drawn from a real failure in `loop_runs` rather than
imagined. The database held Robbbbie Carter, Nia Coleman, two Katies and
fourteen past calendar events, and the loop told the user it had none of them.
"""
from __future__ import annotations

import types
from datetime import timedelta

import pytest

from lifeline import db
from lifeline.assistant import loop, registry, tools
from lifeline.extraction import providers
from lifeline.models import CalendarEvent, Message, Person, new_id

from tests.conftest import NOW, days_from_now, make_conversation, make_item, make_message, make_person


# --------------------------------------------------------- find_person v2
def test_finds_a_person_through_a_misspelling():
    """The exact miss: a name typed one letter out answered "I don't have
    them on file", while `Robbbbie 😛👅 Carter` sat in the database — plus
    emoji the old substring scan choked on."""
    make_person("robbbbie-carter", "Robbbbie 😛👅 Carter")
    hit = tools.find_person("robbbie")
    assert hit is not None
    assert hit["person_id"] == "robbbbie-carter"


def test_finds_a_person_by_first_name_only():
    make_person("nia-coleman", "Nia Coleman")
    assert tools.find_person("Nia")["person_id"] == "nia-coleman"


def test_two_people_with_the_same_first_name_come_back_ambiguous():
    """The database has a Katie Bishop and a Katie Marsh. Answering "Katie"
    with one of them silently is how a draft reaches the wrong person — and
    "who is her?" is what the loop actually said instead."""
    make_person("katie-bishop", "Katie Bishop")
    make_person("katie-marsh", "Katie Marsh")
    hit = tools.find_person("Katie")
    assert hit["ambiguous"] is True
    assert {a["name"] for a in hit["alternatives"]} == {"Katie Bishop"} | {"Katie Marsh"} - {hit["name"]}


def test_a_full_name_is_not_ambiguous():
    make_person("katie-bishop", "Katie Bishop")
    make_person("katie-marsh", "Katie Marsh")
    hit = tools.find_person("Katie Bishop")
    assert hit["person_id"] == "katie-bishop"
    assert hit.get("ambiguous") is not True


def test_phone_numbers_match_however_they_are_written():
    make_person("p1", "Someone", handles=["+16465550149"])
    for spelling in ["6465550149", "+1 646 555 0149", "(646) 555-0149", "646-555-0149"]:
        assert tools.find_person(spelling)["person_id"] == "p1", spelling


def test_email_matches_case_insensitively():
    make_person("p1", "Someone", handles=["Marcus.ReedJr9@gmail.com"])
    assert tools.find_person("marcus.reedjr9@GMAIL.com")["person_id"] == "p1"


def test_nonsense_still_returns_nothing():
    """Loose matching must not mean matching anything — a confident wrong
    person is worse than an honest miss."""
    make_person("tess", "Tess")
    assert tools.find_person("zzzqqxvv") is None


# ------------------------------------------------------ search_messages v2
def test_search_carries_a_message_id_to_open_in_full():
    make_person()
    make_conversation()
    message = make_message("the water bill is ready and due august 31")
    hit = tools.search_messages(query="water bill")[0]
    assert hit["message_id"] == message.id
    assert tools.get_message(hit["message_id"])["text"] == message.text


def test_search_filters_by_direction_and_source():
    make_person()
    make_conversation()
    make_message("something I said about vets", is_from_user=True)
    make_message("something they said about vets", is_from_user=False)

    mine = tools.search_messages(query="vets", direction="from_you")
    assert [h["sender"] for h in mine] == ["You"]
    theirs = tools.search_messages(query="vets", direction="from_them")
    assert [h["sender"] for h in theirs] == ["Tess"]
    assert tools.search_messages(query="vets", source="gmail") == []


def test_search_filters_by_date_range():
    make_person()
    make_conversation()
    make_message("old news", at=NOW - timedelta(days=30))
    make_message("recent news", at=NOW - timedelta(days=1))
    recent = tools.search_messages(
        query="news", since=(NOW - timedelta(days=7)).isoformat(timespec="seconds")
    )
    assert [h["text"] for h in recent] == ["recent news"]


def test_search_can_ask_for_links_with_no_keywords():
    """"What did someone send me a link to?" has no keyword to search for —
    the old signature required one and returned nothing."""
    make_person()
    make_conversation()
    make_message("check this https://example.com/tickets")
    make_message("no link here")
    hits = tools.search_messages(has_link=True)
    assert len(hits) == 1 and "example.com" in hits[0]["text"]


def test_search_with_nothing_asked_returns_nothing():
    make_person()
    make_conversation()
    make_message("anything")
    assert tools.search_messages() == []


# ------------------------------------------------------------ get_message
def test_get_message_is_not_truncated():
    """`read_conversation` cuts at 400 characters and search at 280 — which is
    where a bill's amount and an itinerary's flight number live."""
    make_person()
    make_conversation()
    body = "Your itinerary. " + ("x" * 600) + " FLIGHT UA1287"
    message = make_message(body)
    assert tools.search_messages(query="itinerary")[0]["truncated"] is True
    assert tools.get_message(message.id)["text"].endswith("FLIGHT UA1287")


def test_get_message_on_a_bad_id_is_a_miss_not_a_crash():
    assert tools.get_message("nope") is None


# ------------------------------------------------------------- search_mail
def _mail(subject, from_email, labels, text="body", at=None, is_from_user=False):
    make_conversation("gmail:t1", source="gmail", name="Mail")
    message = Message(
        id=new_id(), source="gmail", conversation_id="gmail:t1", external_id=new_id(),
        person_id=None, is_from_user=is_from_user,
        timestamp=(at or NOW).isoformat(timespec="seconds"),
        text=f"{subject}\n{text}",
        metadata={"subject": subject, "from_email": from_email, "labels": labels},
    )
    db.insert_messages([message])
    return message


def test_mail_search_by_sender_domain():
    """Bills and confirmations are identified by who sent them. The metadata
    was already stored and had no way to be queried."""
    _mail("Your statement", "no-reply@notification.capitalone.com", ["INBOX"])
    _mail("Weekend plans", "friend@gmail.com", ["INBOX"])
    hits = tools.search_mail(sender="capitalone")
    assert len(hits) == 1 and hits[0]["subject"] == "Your statement"


def test_mail_search_by_label():
    _mail("Flagged", "a@b.com", ["IMPORTANT", "INBOX"])
    _mail("Not flagged", "c@d.com", ["INBOX"])
    assert [h["subject"] for h in tools.search_mail(label="IMPORTANT")] == ["Flagged"]


def test_mail_search_ignores_imessage():
    make_person()
    make_conversation()
    make_message("this is an imessage about statements")
    _mail("statements", "a@b.com", ["INBOX"])
    assert all(h["source"] == "gmail" for h in tools.search_mail(query="statements"))


# ---------------------------------------------------------------- timeline
def test_timeline_interleaves_every_channel_in_order():
    """The cross-channel view the thesis rests on. One source at a time gives
    half the picture."""
    make_person()
    make_conversation()
    make_message("about the trip", at=NOW - timedelta(days=5))
    make_item(text="book the flights for the trip", at=NOW - timedelta(days=3))
    db.upsert_calendar_events([
        CalendarEvent(id="e1", calendar_id="c", summary="trip departure",
                      start_at=(NOW - timedelta(days=1)).isoformat(timespec="seconds"))
    ])

    entries = tools.timeline(query="trip", days=365)
    assert [e["kind"] for e in entries] == ["message", "item", "calendar"]
    assert entries == sorted(entries, key=lambda e: e["at"])


# ---------------------------------------------------------- search_history
def test_history_shows_what_closed_and_why():
    from lifeline.completion import engine

    make_person()
    make_conversation()
    item = make_item(text="pay the water bill")
    engine.manual_close(item.id)

    past = tools.search_history(query="water")
    assert len(past) == 1
    assert past[0]["status"] == "completed"
    assert past[0]["closed_by"] == "manual"


def test_history_excludes_things_still_open():
    make_person()
    make_conversation()
    make_item(text="still open and unresolved")
    assert tools.search_history(query="unresolved") == []


# ------------------------------------------------- the starting rule (loop)
def _provider(script):
    turns = list(script)

    def complete_with_tools(messages, *, tools, system=None, max_tokens=1024):
        return turns.pop(0) if turns else {"text": "out of script", "tool_calls": []}

    return types.SimpleNamespace(__name__="fake", complete_with_tools=complete_with_tools)


def _echo():
    return registry.Tool(
        name="echo", description="echo",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        fn=lambda q="": {"echo": q},
    )


@pytest.mark.parametrize("excuse", [
    "I don't have booooby on file. Do you have their full name?",
    "I don't have access to historical calendar data from yesterday.",
    "I'd be happy to help, but I need to know who \"her\" is.",
    "Are you asking about Katie — who she is, or what you owe her?",
])
def test_a_conclusion_with_no_tool_calls_is_sent_back_to_look(monkeypatch, excuse):
    """Each of these is verbatim from a real run that called nothing and was
    wrong. `CONVERSE_SYSTEM` already forbids this in prose; it kept happening,
    so the rule is structural."""
    monkeypatch.setattr(providers, "available", lambda: [_provider([
        {"text": excuse, "tool_calls": []},
        {"text": "", "tool_calls": [{"id": "c1", "name": "echo", "input": {"q": "looking"}}]},
        {"text": "Found them — Robbbbie Carter.", "tool_calls": []},
    ])])
    run = loop.run_loop("who is booooby", trigger="converse", tools=[_echo()])
    assert run.conclusion == "Found them — Robbbbie Carter."
    assert [c["name"] for c in run.tool_calls] == ["echo"]


def test_an_honest_miss_after_looking_is_accepted(monkeypatch):
    """A run that searched and found nothing is allowed to say so. The rule
    targets not looking, not the answer 'no'."""
    monkeypatch.setattr(providers, "available", lambda: [_provider([
        {"text": "", "tool_calls": [{"id": "c1", "name": "echo", "input": {"q": "x"}}]},
        {"text": "I don't have anything on file about that.", "tool_calls": []},
    ])])
    run = loop.run_loop("anything about x", trigger="converse", tools=[_echo()])
    assert run.conclusion == "I don't have anything on file about that."
    assert run.iterations == 2


def test_a_normal_answer_is_not_second_guessed(monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: [_provider([
        {"text": "You owe Tess a call about the vet.", "tool_calls": []},
    ])])
    run = loop.run_loop("what do I owe Tess", trigger="converse", tools=[_echo()])
    assert run.iterations == 1


def test_the_loop_gives_up_after_one_nudge(monkeypatch):
    """A model that still won't look after being told isn't going to on the
    third ask — and an unbounded retry would burn the budget on stubbornness."""
    monkeypatch.setattr(providers, "available", lambda: [_provider([
        {"text": "I don't have that on file.", "tool_calls": []},
        {"text": "I still don't have that on file.", "tool_calls": []},
    ])])
    run = loop.run_loop("who is x", trigger="converse", tools=[_echo()])
    assert run.conclusion == "I still don't have that on file."
    assert run.iterations == 2


def test_the_nudge_is_counted_so_the_rule_can_be_measured(monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: [_provider([
        {"text": "I can't find anything.", "tool_calls": []},
        {"text": "Looked properly this time.", "tool_calls": []},
    ])])
    before = int(db.get_sync_state("loop:nudges") or "0")
    loop.run_loop("go", trigger="converse", tools=[_echo()])
    assert int(db.get_sync_state("loop:nudges")) == before + 1


def test_tier1_tools_are_all_registered():
    names = {t.name for t in registry.READ_TOOLS}
    assert {"search_messages", "find_person", "search_calendar", "quiet_conversations",
            "list_open_items", "get_message", "search_mail", "timeline",
            "search_history"} <= names


# ------------------------------------------------------- search_calendar v2
def test_calendar_can_reach_into_the_past():
    """"What appointments did I have yesterday" was answered "I don't have
    access to historical calendar data" while fourteen past events sat in the
    table. The tool couldn't see them, so the model concluded they didn't
    exist — a limit that reads as a lie."""
    past = (NOW - timedelta(days=10)).isoformat(timespec="seconds")
    future = (NOW + timedelta(days=10)).isoformat(timespec="seconds")
    db.upsert_calendar_events([
        CalendarEvent(id="old", calendar_id="c", summary="Dentist", start_at=past),
        CalendarEvent(id="new", calendar_id="c", summary="Flight", start_at=future),
    ])
    upcoming = {e["summary"] for e in tools.search_calendar()}
    assert "Dentist" not in upcoming            # default is still upcoming-only

    widened = {e["summary"] for e in tools.search_calendar(since=past)}
    assert "Dentist" in widened and "Flight" in widened


def test_calendar_window_can_be_bounded_at_both_ends():
    past = (NOW - timedelta(days=10)).isoformat(timespec="seconds")
    future = (NOW + timedelta(days=10)).isoformat(timespec="seconds")
    db.upsert_calendar_events([
        CalendarEvent(id="old", calendar_id="c", summary="Dentist", start_at=past),
        CalendarEvent(id="new", calendar_id="c", summary="Flight", start_at=future),
    ])
    only_past = tools.search_calendar(
        since=past, until=(NOW - timedelta(days=1)).isoformat(timespec="seconds")
    )
    assert [e["summary"] for e in only_past] == ["Dentist"]


# ---------------------------------------------------------- knowing the date
def _flat(system) -> str:
    """The system prompt as the model reads it.

    It travels as blocks now so the provider can put a cache breakpoint
    between the stable prompt and the volatile date stamp; these tests care
    about the text reaching the model, which the split does not change.
    """
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(part["text"] for part in system if part.get("text"))


def test_the_loop_tells_the_model_what_day_it_is():
    """Asked "what appointments did I have in July?" the loop searched
    correctly and answered "no events on file for July 2024" — with a July
    2026 event in the table. It had never been told the year."""
    from datetime import datetime, timezone

    stamped = _flat(loop._dated("You are an assistant."))
    assert datetime.now(timezone.utc).date().isoformat() in stamped
    assert "You are an assistant." in stamped


def test_the_date_is_added_even_with_no_system_prompt():
    from datetime import datetime, timezone

    assert datetime.now(timezone.utc).date().isoformat() in _flat(loop._dated(None))


def test_the_provider_actually_receives_the_date(monkeypatch):
    from datetime import datetime, timezone

    seen = {}

    def complete_with_tools(messages, *, tools, system=None, max_tokens=1024):
        seen["system"] = system
        return {"text": "done", "tool_calls": []}

    monkeypatch.setattr(providers, "available", lambda: [
        types.SimpleNamespace(__name__="fake", complete_with_tools=complete_with_tools)
    ])
    loop.run_loop("hi", trigger="ask", tools=[_echo()], system="BE HELPFUL")
    assert datetime.now(timezone.utc).date().isoformat() in _flat(seen["system"])
    assert "BE HELPFUL" in _flat(seen["system"])


# ------------------------------------------------- drafts move to write-time
def test_draft_brief_carries_the_thread_its_evidence_and_its_people():
    """The whole difference. An extraction-time reply sees one message and
    knows nothing about the loop — which is how 33 items in the live database
    got drafted "yep, I'll take care of it", four addressed to billing robots."""
    from lifeline import threads
    from lifeline.models import Evidence

    make_person("robbie", "Robbie")
    make_conversation()
    item = make_item(person_id="robbie", person="Robbie", text="let me know when you're out")
    thread = threads.create(
        title="Sort out Robbie's message", summary="he asked and you never said",
        evidence=[Evidence(kind="item", ref_id=item.id, role="founding")],
    )

    brief = threads.draft_brief(thread.id)
    assert brief["title"] == "Sort out Robbie's message"
    assert brief["people"][0]["name"] == "Robbie"
    assert "looks_automated" in brief["people"][0]
    assert brief["evidence"][0]["what"]


def test_draft_brief_flags_an_automated_counterpart():
    """American Water, Capital One and Fidelity all had "yep, I'll take care
    of it" queued as a reply to a no-reply address."""
    from lifeline import threads
    from lifeline.models import Evidence

    make_person("american-water", "American Water", handles=["customer_service@cs.amwater.com"])
    make_conversation()
    item = make_item(person_id="american-water", person="American Water", text="your bill is ready")
    thread = threads.create(title="Pay the water bill",
                            evidence=[Evidence(kind="item", ref_id=item.id)])
    assert threads.draft_brief(thread.id)["people"][0]["looks_automated"] is True


def test_draft_endpoint_returns_the_written_message(monkeypatch):
    from fastapi.testclient import TestClient

    from lifeline import threads
    from lifeline.api.app import app
    from lifeline.models import Evidence

    make_person("robbie", "Robbie", handles=["+19175550187"])
    make_conversation()
    item = make_item(person_id="robbie", person="Robbie", text="let me know when you're out")
    thread = threads.create(title="Get back to Robbie",
                            evidence=[Evidence(kind="item", ref_id=item.id)])

    monkeypatch.setattr(providers, "available", lambda: [_provider([
        {"text": "", "tool_calls": [{"id": "c1", "name": "draft_message",
                                     "input": {"person_id": "robbie", "text": "just got out, all good"}}]},
        {"text": "Drafted a short reply to Robbie.", "tool_calls": []},
    ])])
    body = TestClient(app).post(f"/threads/{thread.id}/draft").json()
    assert body["draft"]["text"] == "just got out, all good"
    assert body["draft"]["person"] == "Robbie"


def test_draft_endpoint_reports_a_refusal_rather_than_inventing_one(monkeypatch):
    """A bill gets paid and a booking gets checked; neither is answered by
    writing to a no-reply address. Saying so beats a draft nobody can send."""
    from fastapi.testclient import TestClient

    from lifeline import threads
    from lifeline.api.app import app

    thread = threads.create(title="Pay the water bill")
    monkeypatch.setattr(providers, "available", lambda: [_provider([
        {"text": "This is an automated billing notice — pay the bill instead.",
         "tool_calls": []},
    ])])
    body = TestClient(app).post(f"/threads/{thread.id}/draft").json()
    assert body["draft"] is None
    assert "pay the bill" in body["reason"].lower()


def test_extraction_no_longer_writes_generic_replies():
    """§v2 decision: drafts move to write-time. The old default produced 33
    copies of "yep, I'll take care of it"."""
    from lifeline.config import get_config

    assert get_config().draft_replies is False
