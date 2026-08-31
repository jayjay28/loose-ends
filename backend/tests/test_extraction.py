"""Extraction pipeline (§5)."""
from __future__ import annotations

from datetime import timedelta

from conftest import NOW, make_item, make_message, make_person, make_conversation

from lifeline import db
from lifeline.extraction import heuristic, pipeline
from lifeline.ingestion import load_sample_corpus
from lifeline.models import ITEM_TYPES


def setup_people():
    make_conversation()
    make_person("tess", "Tess", "spouse")


# ------------------------------------------------------------ classifier
def test_taxonomy_examples_from_the_spec():
    batch = [
        {"id": "1", "source": "imessage", "person": "Tess", "timestamp": NOW.isoformat(), "text": "I want that bag"},
        {"id": "2", "source": "imessage", "person": "Mom", "timestamp": NOW.isoformat(), "text": "Grandma's 80th is in 3 weeks"},
        {"id": "3", "source": "imessage", "person": "Sara", "timestamp": NOW.isoformat(), "text": "I'll get the hoop for his birthday"},
        {"id": "4", "source": "imessage", "person": "Mom", "timestamp": NOW.isoformat(), "text": "Did you look at that paper I sent?"},
        {"id": "5", "source": "whatsapp", "person": "Dev", "timestamp": NOW.isoformat(), "text": "Found this article, you'd love it https://x.com/a"},
    ]
    by_id = {r["message_id"]: r for r in heuristic.classify_batch(batch)["items"]}
    assert by_id["1"]["type"] == "purchase"
    assert by_id["2"]["type"] == "event"
    assert by_id["3"]["type"] == "promise"
    assert by_id["4"]["type"] == "followup"
    assert by_id["5"]["type"] == "reading"
    assert by_id["5"]["entities"]["link"] == "https://x.com/a"


def test_chatter_yields_nothing():
    batch = [
        {"id": "1", "source": "imessage", "person": "Tess", "timestamp": NOW.isoformat(), "text": "ha, noted"},
        {"id": "2", "source": "imessage", "person": "Tess", "timestamp": NOW.isoformat(), "text": "ok"},
        {"id": "3", "source": "imessage", "person": "Tess", "timestamp": NOW.isoformat(), "text": "👍"},
    ]
    assert heuristic.classify_batch(batch)["items"] == []


def test_automated_mail_is_not_an_item():
    batch = [
        {
            "id": "1",
            "source": "mail",
            "person": "Fillmore Hardware",
            "timestamp": NOW.isoformat(),
            "text": "20% off all power tools this weekend — please visit us",
            "metadata": {"from_email": "newsletter@fillmorehardware.com", "labels": ["CATEGORY_PROMOTIONS"]},
        }
    ]
    assert heuristic.classify_batch(batch)["items"] == []


def test_flexible_language_lowers_confidence():
    flexible = heuristic.classify_batch(
        [{"id": "1", "source": "imessage", "person": "Theo", "timestamp": NOW.isoformat(), "text": "no rush, but can you send the file"}]
    )
    firm = heuristic.classify_batch(
        [{"id": "2", "source": "imessage", "person": "Theo", "timestamp": NOW.isoformat(), "text": "can you send the file, deadline is today"}]
    )
    assert flexible["items"][0]["confidence"] < firm["items"][0]["confidence"]


def test_replies_can_be_disabled():
    result = heuristic.classify_batch(
        [{"id": "1", "source": "imessage", "person": "Tess", "timestamp": NOW.isoformat(), "text": "can you call the vet"}],
        draft_replies=False,
    )
    assert result["items"][0]["suggested_reply"] is None


# -------------------------------------------------------------- pipeline
def test_pipeline_skips_the_users_own_messages():
    setup_people()
    make_message("can you call the vet tomorrow", is_from_user=True, person_id=None)
    assert pipeline.run() == []


def test_pipeline_is_incremental():
    setup_people()
    make_message("can you call the vet tomorrow")
    first = pipeline.run()
    assert len(first) == 1
    assert pipeline.run() == [], "already-extracted messages must not be re-processed"

    make_message("did you ever call the vet", at=NOW + timedelta(days=1))
    assert len(pipeline.run()) == 1


def test_extracted_items_match_the_section_5_schema():
    setup_people()
    make_message("can you order the new filters by Friday")
    item = pipeline.run()[0]
    payload = item.to_spec_dict()
    assert set(payload) == {
        "id", "source", "conversation_id", "person", "timestamp", "type", "raw_text",
        "entities", "suggested_action", "suggested_reply", "status", "created_at",
    }
    assert set(payload["entities"]) == {"item", "date", "link"}
    assert payload["type"] in ITEM_TYPES
    assert payload["status"] == "pending"


def test_relative_dates_are_anchored_to_the_message_not_now():
    setup_people()
    long_ago = NOW - timedelta(days=10)
    make_message("the deadline is in 3 days", at=long_ago)
    item = pipeline.run()[0]
    assert item.entities.date.startswith("2026-07-20"), item.entities.date


def test_followups_link_back_to_the_earlier_item():
    setup_people()
    make_message("can you order the tubeless tyre kit for the bike")
    pipeline.run()
    make_message("did you ever order the tubeless tyre kit", at=NOW + timedelta(days=2))
    followup = [i for i in pipeline.run() if i.type == "followup"]
    assert followup and followup[0].links_to_item_id is not None


def test_items_are_scored_on_creation():
    setup_people()
    make_message("can you call the vet, it's urgent, by tomorrow")
    item = pipeline.run()[0]
    assert item.score != 0.0
    assert item.interruption_level in ("time_sensitive", "active", "passive")


def test_partial_reruns_do_not_duplicate_items():
    setup_people()
    message = make_message("can you call the vet tomorrow")
    pipeline.run()
    # Simulate a crash after item creation but before the watermark was set.
    db.get_connection().execute("UPDATE messages SET extracted_at = NULL WHERE id = ?", (message.id,))
    db.get_connection().commit()
    assert pipeline.run() == []
    assert len(db.list_items()) == 1


def test_end_to_end_on_the_sample_corpus(sample_dir):
    load_sample_corpus(sample_dir)
    items = pipeline.run()
    assert len(items) >= 15
    assert {i.source for i in items} == {"imessage", "whatsapp", "mail"}
    assert all(i.type in ITEM_TYPES for i in items)
    # The spec's worked examples must survive the whole pipeline.
    actions = " | ".join(i.suggested_action.lower() for i in items)
    assert "lemaire croissant bag" in actions
    assert "grandma's 80th" in actions
    # Receipts and promotional mail are evidence, not obligations.
    assert not any("20% off" in i.raw_text for i in items)
    assert not any("UQXBTR" in i.raw_text for i in items)


def test_one_malformed_item_costs_one_item_never_the_batch(monkeypatch):
    """Audit finding #5. A string confidence, a string entities, an invented
    link id — any of them used to raise out of pipeline.run(), which killed
    the cycle before completion/closure/worker ran, and the same batch was
    re-sent (and re-billed) next cycle while extracting nothing."""
    from lifeline.extraction import pipeline, providers

    setup_people()
    m1 = make_message("can you pay the water bill", person_id="tess")
    m2 = make_message("can you call the vet", person_id="tess")

    monkeypatch.setattr(providers, "run", lambda fn, kind=None, **kw: {
        "items": [
            {"message_id": m1.id, "type": "promise", "confidence": "very",
             "entities": "not a dict", "suggested_action": "??"},          # garbage
            {"message_id": m2.id, "type": "promise", "confidence": 0.9,
             "entities": {"item": "call the vet"}, "suggested_action": "Call the vet",
             "suggested_reply": {"not": "a string"}},                       # coercible
        ],
        "entities": [],
    })
    created = pipeline.run(rescore=False)

    assert len(created) == 1, "the good item landed"
    assert created[0].suggested_action == "Call the vet"
    assert created[0].suggested_reply is None, "non-string reply coerced away"
    # And the batch was consumed: nothing pending to re-bill next cycle.
    assert db.unextracted_messages() == []


def test_a_missing_confidence_is_not_certainty(monkeypatch):
    """`raw.get("confidence", 1.0)` treated the model's silence as 1.0 —
    sailing past MIN_CONFIDENCE. No confidence is no item."""
    from lifeline.extraction import pipeline, providers

    setup_people()
    m = make_message("hmm maybe pajamas?", person_id="tess")
    monkeypatch.setattr(providers, "run", lambda fn, kind=None, **kw: {
        "items": [{"message_id": m.id, "type": "purchase",
                   "entities": {}, "suggested_action": "Buy pajamas"}],
        "entities": [],
    })
    assert pipeline.run(rescore=False) == []


def test_an_invented_link_id_is_no_link(monkeypatch):
    from lifeline.extraction import pipeline, providers

    setup_people()
    earlier = make_item(item_type="promise", text="send the lease over")
    followup = make_item(item_type="followup", text="did you send that lease?")

    calls = {}
    def fake_run(fn, kind=None, **kw):
        calls["ran"] = True
        return "no-such-item-id"
    monkeypatch.setattr(providers, "run", fake_run)

    assert pipeline._link_followup(followup) is None, \
        "an id the model was never offered is no link"


def test_a_heuristic_batch_stays_open_for_the_model(monkeypatch):
    """Audit finding #4. Budget spent or every provider down → the heuristic
    classified the batch and the messages were stamped extracted forever; the
    LLM never saw them. "We still owe the sitter for last weekend" was
    consumed with zero items and never revisited after the cap reset."""
    from lifeline.extraction import pipeline, providers

    setup_people()
    subtle = make_message("we still owe the sitter for last weekend", person_id="tess")

    # Providers configured, but the chain returns None (budget/failure).
    monkeypatch.setattr(providers, "run", lambda fn, kind=None, **kw: None)
    monkeypatch.setattr(providers, "available", lambda: ["claude-is-configured"])
    created = pipeline.run(rescore=False)

    pending = {m.id for m in db.unextracted_messages()}
    assert subtle.id in pending, "the batch stays open for the model"

    # Providers recover: the model reads the same message and finds the loop.
    monkeypatch.setattr(providers, "run", lambda fn, kind=None, **kw: {
        "items": [{"message_id": subtle.id, "type": "promise", "confidence": 0.8,
                   "entities": {"item": "pay the sitter"},
                   "suggested_action": "Pay the sitter for last weekend"}],
        "entities": [],
    })
    created = pipeline.run(rescore=False)
    assert [i.suggested_action for i in created] == ["Pay the sitter for last weekend"]
    assert db.unextracted_messages() == []


def test_with_no_provider_at_all_the_rules_are_the_reader(monkeypatch):
    """No key configured: the heuristic is the final answer, and holding the
    batch open would just re-run the same rules forever."""
    from lifeline.extraction import pipeline, providers

    setup_people()
    make_message("can you send the file, deadline is today", person_id="tess")
    monkeypatch.setattr(providers, "run", lambda fn, kind=None, **kw: None)
    monkeypatch.setattr(providers, "available", lambda: [])

    created = pipeline.run(rescore=False)
    assert len(created) == 1, "the heuristic still extracts"
    assert db.unextracted_messages() == [], "and the batch is consumed"
