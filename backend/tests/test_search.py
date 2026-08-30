"""The retrieval floor (§v2.8 phase 1).

`search_messages` was six `LIKE '%term%'` clauses: no word boundaries, no
ranking, no reach into attachments. Searching a child's name returned a
term-life-insurance ad above her school's email, and the loop learned that
searching was useless — which is half of why `/ask` answered "I couldn't find
anything" to questions the store could answer. These tests are the diagnosis's
exact failures, pinned.
"""
from __future__ import annotations

from datetime import timedelta

from lifeline import db
from lifeline.assistant import tools
from lifeline.models import Attachment

from tests.conftest import NOW, make_conversation, make_message, make_person


def _mail(text, external_id, subject="", days_ago=1.0, conversation_id="gmail:t1"):
    return make_message(
        text, conversation_id=conversation_id, person_id=None,
        at=NOW - timedelta(days=days_ago),
        metadata={"subject": subject} if subject else {},
        external_id=external_id, source="gmail",
    )


def test_a_name_finds_the_person_not_the_advertisement():
    """The live failure: `search_messages("Lia")` returned "Term life
    insurance, made for busy people." — 'lia' is a substring of both. FTS
    matches words."""
    make_conversation("gmail:t1", source="gmail", name="school")
    _mail("Term life insurance, made for busy people.", "g-ad")
    _mail("Please see the updated universal form and Lia's action plan attached.",
          "g-plan", subject="Re: Asthma Action Plan")

    hits = tools.search_messages(query="Lia")
    assert len(hits) == 1
    assert hits[0]["message_id"] != "", "shape intact"
    assert "Lia" in hits[0]["text"]


def test_more_matching_words_rank_higher():
    make_conversation("gmail:t1", source="gmail", name="school")
    _mail("the preschool sent the calendar", "g-one")
    _mail("Lia's preschool registration calendar and supply list", "g-both")

    hits = tools.search_messages(query="Lia preschool calendar")
    assert [h["message_id"] for h in hits][0] == \
        db.get_message_by_external_id("gmail", "g-both").id


def test_a_hit_inside_a_pdf_surfaces_as_its_message():
    """Phase 0 put the cargo in the store; the search has to reach it. The
    account number below exists nowhere in any message body."""
    make_conversation("gmail:t1", source="gmail", name="American Water")
    carrier = _mail("An important message regarding your account", "g-water")
    db.insert_attachment(Attachment(
        message_id=carrier.id, source="gmail", filename="AutoPay Failure.pdf",
        mime="application/pdf", sha256="sha-water",
        text="AutoPay Failure for account 210055500123. Amount due $84.20.",
        parsed_at="2026-08-28T00:00:00+00:00",
    ))

    hits = tools.search_messages(query="210055500123")
    assert len(hits) == 1
    assert hits[0]["message_id"] == carrier.id
    assert hits[0]["via_attachment"] == "AutoPay Failure.pdf"


def test_filters_still_bind_on_fts_hits():
    make_person("nia", "Nia", relationship="partner")
    make_conversation("imessage:t1", name="Nia")
    make_conversation("gmail:t1", source="gmail", name="school")
    make_message("the preschool called about pickup", person_id="nia",
                 at=NOW - timedelta(days=2))
    _mail("preschool newsletter for families", "g-news", days_ago=40)

    only_imessage = tools.search_messages(query="preschool", source="imessage")
    assert {h["source"] for h in only_imessage} == {"imessage"}

    recent = tools.search_messages(query="preschool",
                                   since=(NOW - timedelta(days=7)).isoformat())
    assert [h["source"] for h in recent] == ["imessage"]


def test_punctuation_in_a_query_cannot_break_the_match():
    make_conversation("gmail:t1", source="gmail", name="school")
    _mail("the water meter application", "g-x")
    # would be an FTS5 syntax error if passed raw
    hits = tools.search_messages(query='water AND) "meter OR (')
    assert len(hits) == 1


def test_stored_history_is_searchable_after_the_migration():
    """External-content FTS starts empty; the migration's 'rebuild' step must
    index what was inserted before the tables existed. The conftest DB is
    fresh (schema.sql + triggers), so this guards the trigger path — the
    migration walk itself is covered by test_migrations' column comparison."""
    make_conversation("gmail:t1", source="gmail", name="school")
    _mail("an older message about the dentist", "g-old", days_ago=80)
    assert tools.search_messages(query="dentist") != []


def test_style_payloads_no_longer_wear_the_text_column():
    from lifeline.ingestion import gmail

    html = ("<html><head><style>body{color:red} .mso{mso-hide:all}</style></head>"
            "<body><!--[if mso]>conditional junk<![endif]-->"
            "<p>Back to school night is Tuesday</p></body></html>")
    import base64
    payload = {"mimeType": "text/html",
               "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()}}
    text = gmail.extract_body(payload)
    assert "Back to school night is Tuesday" in text
    assert "color:red" not in text
    assert "conditional junk" not in text
    assert "mso" not in text
