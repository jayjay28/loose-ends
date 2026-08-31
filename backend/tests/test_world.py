"""The world model (§v2.8 phase 2).

After 3,640 messages the store held 252 people with zero relationships, and
54 facts whose predicate and value columns were NULL on every row. These tests
pin the three promises the entity layer makes: names resolve, facts carry
receipts and supersede rather than delete, and nothing new gets a slug id.
"""
from __future__ import annotations

from lifeline import db, world
from lifeline.models import Person

from tests.conftest import make_conversation, make_message, make_person


# ---------------------------------------------------------------- identity

def test_a_person_is_an_entity_under_every_handle():
    make_person("nora", "Nora Carter", relationship=None,
                handles=["+1 (917) 555-0142", "Nora@example.com"])

    for handle in ["Nora Carter", "nora carter", "+19175550142", "917-555-0142",
                   "nora@example.com"]:
        entity = world.resolve(handle)
        assert entity is not None, handle
        assert entity.id == "nora"
        assert entity.kind == "person"


def test_new_entities_get_opaque_ids_and_dedupe_by_alias():
    """slugify(display_name) made two Mikes one person. Opaque ids are what
    make a future merge an alias rewrite instead of a DELETE."""
    school = world.upsert("org", "Lakeview Public Schools")
    assert school.id != "lakeview-public-schools"
    assert len(school.id) == 36, "a uuid, not a slug"

    again = world.upsert("org", "Lakeview Public Schools")
    assert again.id == school.id, "the alias index deduplicates"


def test_renaming_a_person_updates_the_entity_and_keeps_old_aliases():
    make_person("tess", "Tess", relationship="spouse")
    db.upsert_person(Person(id="tess", display_name="Tess Carter",
                            relationship="spouse", handles=[]))

    assert world.resolve("Tess Carter").id == "tess"
    assert world.resolve("Tess").id == "tess", "the old name still answers"
    assert world.resolve("tess carter").name == "Tess Carter"


# -------------------------------------------------------------------- facts

def test_a_fact_carries_its_receipt():
    make_person("nora", "Nora Carter", relationship=None)
    make_conversation("gmail:t1", source="gmail", name="school")
    message = make_message("Nora's action plan attached", conversation_id="gmail:t1",
                           person_id=None, external_id="g-plan", source="gmail")

    fact = world.record_fact("nora", "attends",
                             "Lakeview Public Schools preschool",
                             message_id=message.id, confidence=0.9)
    stored = world.facts_for("nora")
    assert len(stored) == 1
    assert stored[0].message_id == message.id, "the receipt"
    assert stored[0].value == "Lakeview Public Schools preschool"


def test_the_same_claim_refreshes_a_different_one_supersedes():
    make_person("milo", "Milo", relationship=None)

    first = world.record_fact("milo", "attends", "Maple Hollow Elementary")
    again = world.record_fact("milo", "attends", "maple hollow elementary")
    assert again.id == first.id, "same claim, same row, fresher last_seen"

    world.record_fact("milo", "attends", "Cedar Grove Middle School")
    active = world.facts_for("milo")
    assert [f.value for f in active] == ["Cedar Grove Middle School"]

    history = world.facts_for("milo", include_superseded=True)
    assert len(history) == 2, "superseded, never deleted"
    assert {f.status for f in history} == {"active", "superseded"}


# --------------------------------------------------------------- resolution

def test_a_question_resolves_to_the_entities_it_mentions():
    """The step that runs in front of retrieval: 'Where is Nora's daycare?'
    must know 'Nora' is a person before any search happens."""
    make_person("nora", "Nora Carter", relationship=None)
    make_person("milo", "Milo", relationship=None)
    world.upsert("org", "Lakeview Public Schools")

    found = world.mentioned_in("Where is Nora's daycare?")
    assert [e.id for e in found] == ["nora"]

    found = world.mentioned_in("did Lakeview Public Schools email about Milo")
    assert {e.name for e in found} == {"Lakeview Public Schools", "Milo"}

    assert world.mentioned_in("nothing known here") == []


def test_the_migration_populates_from_existing_people(tmp_path):
    """A database from before phase 2 walks forward and its people answer to
    their names and handles."""
    import sqlite3

    from lifeline.db import MIGRATIONS, migrate

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # minimal pre-phase-2 shape: the people table, then every migration
    conn.execute("""CREATE TABLE people (
        id TEXT PRIMARY KEY, display_name TEXT NOT NULL, relationship TEXT,
        handles TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO people VALUES ('nia', 'Nia Coleman', NULL, "
                 "'[\"+19175550187\", \"nia@example.com\"]', '2026-08-01T00:00:00+00:00')")
    entities_step = next(
        i for i, step in enumerate(MIGRATIONS)
        if any(getattr(x, "__name__", "") == "_people_become_entities" for x in step)
    )
    conn.execute(f"PRAGMA user_version = {entities_step}")   # everything before phase 2
    conn.commit()
    migrate(conn)

    row = conn.execute(
        "SELECT e.id FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
        "WHERE a.alias = '9175550187'"
    ).fetchone()
    assert row["id"] == "nia"
    row = conn.execute("SELECT kind, name FROM entities WHERE id='nia'").fetchone()
    assert (row["kind"], row["name"]) == ("person", "Nia Coleman")


# --------------------------------------------- phase 3: the second output

def _classifier_says(monkeypatch, payload):
    """Pin the provider layer to a fixed response."""
    from lifeline.extraction import pipeline, providers

    monkeypatch.setattr(providers, "run", lambda fn, kind=None, **kw: payload)
    return pipeline


def test_a_message_about_lia_creates_her(monkeypatch):
    """The whole point of the phase. Nora has never sent a message, so no
    ingestion path could ever create her — but the preschool nurse's email
    *states* who she is, and the pass that reads it now has somewhere to put
    that."""
    from lifeline import world

    make_conversation("gmail:t1", source="gmail", name="Rosa Alvarez")
    make_person("rosemary", "Rosa Alvarez", relationship=None)
    message = make_message(
        "Please see the updated universal form and Nora's action plan attached.",
        conversation_id="gmail:t1", person_id="rosemary",
        external_id="g-nurse", source="gmail",
    )
    pipeline = _classifier_says(monkeypatch, {
        "items": [],
        "entities": [
            {"message_id": message.id, "name": "Nora", "kind": "person",
             "confidence": 0.9,
             "claims": [
                 {"predicate": "attends", "value": "Lakeview Public Schools preschool"},
                 {"predicate": "relation_to_user", "value": "daughter"},
             ]},
            {"message_id": message.id, "name": "Rosa Alvarez", "kind": "person",
             "claims": [{"predicate": "role_of", "value": "LPS Preschool Nurse"}]},
        ],
    })
    pipeline.run(rescore=False)

    nora = world.resolve("Nora")
    assert nora is not None, "she exists now"
    facts = {f.predicate: f for f in world.facts_for(nora.id)}
    assert facts["attends"].value == "Lakeview Public Schools preschool"
    assert facts["attends"].message_id == message.id, "the receipt"
    # ... and the claim about the nurse landed on her *existing* entity.
    assert world.facts_for("rosemary")[0].value == "LPS Preschool Nurse"


def test_claims_from_bulk_mail_are_refused(monkeypatch):
    """"Every newsletter sender is technically an organisation" is how a
    world model becomes a mailing list."""
    from lifeline import world

    make_conversation("gmail:t1", source="gmail", name="Warby Parker")
    message = make_message(
        "Save on your way back to school", conversation_id="gmail:t1",
        person_id=None, metadata={"promotional": True},
        external_id="g-promo", source="gmail",
    )
    pipeline = _classifier_says(monkeypatch, {
        "items": [],
        "entities": [{"message_id": message.id, "name": "Warby Parker",
                      "kind": "org", "claims": [{"predicate": "located_at", "value": "everywhere"}]}],
    })
    pipeline.run(rescore=False)
    assert world.resolve("Warby Parker") is None


def test_malformed_claims_drop_without_costing_the_items(monkeypatch):
    """The audit's law, applied to the new key: one bad element of the
    response must not kill the batch — and the items in the same response
    must still land."""
    from lifeline import db as _db, world

    make_conversation("imessage:t1", name="Nia")
    make_person("nia", "Nia", relationship="partner")
    message = make_message("can you grab diapers for Nora today",
                           person_id="nia", external_id="im-1")
    pipeline = _classifier_says(monkeypatch, {
        "items": [{"message_id": message.id, "type": "purchase",
                   "entities": {"item": "diapers"}, "suggested_action": "Buy diapers",
                   "confidence": 0.9}],
        "entities": [
            {"message_id": message.id, "name": "", "kind": "person",
             "claims": [{"predicate": "x", "value": "y"}]},          # no name
            {"message_id": message.id, "name": "Nora", "kind": "creature",
             "claims": [{"predicate": "is", "value": "a child"}]},    # bad kind
            {"message_id": message.id, "name": "Nora", "kind": "person",
             "claims": "not a list", "confidence": "very"},           # both coercions
            {"message_id": "unknown", "name": "Ghost", "kind": "person",
             "claims": [{"predicate": "haunts", "value": "nothing"}]},
        ],
    })
    created = pipeline.run(rescore=False)
    assert len(created) == 1, "the item still landed"
    assert created[0].entities.item == "diapers"
    assert world.resolve("Ghost") is None
    # "Nora" with claims="not a list" upserts nothing (no valid claim ever
    # forced the entity into being).
    assert world.resolve("Nora") is None


def test_claim_sprawl_is_capped(monkeypatch):
    from lifeline import world
    from lifeline.extraction import pipeline as pl

    make_conversation("imessage:t1", name="Nia")
    make_person("nia", "Nia", relationship="partner")
    message = make_message("long newsletter-ish text from a real person",
                           person_id="nia", external_id="im-sprawl")
    claims = [
        {"message_id": message.id, "name": f"Org {n}", "kind": "org",
         "claims": [{"predicate": "member_of", "value": "something"}]}
        for n in range(12)
    ]
    pipeline = _classifier_says(monkeypatch, {"items": [], "entities": claims})
    pipeline.run(rescore=False)
    made = [world.resolve(f"Org {n}") for n in range(12)]
    assert sum(1 for e in made if e) == pl.MAX_CLAIMS_PER_MESSAGE


# ------------------------------------------- phase 4: resolution and binding

def test_grounding_turns_a_name_into_search_vocabulary():
    from lifeline import world

    make_person("nora", "Nora Carter", relationship=None)
    world.record_fact("nora", "attends", "Lakeview Public Schools preschool")

    block = world.grounding("Where is Nora's daycare?")
    assert "Nora Carter" in block
    assert "Lakeview Public Schools preschool" in block, \
        "the institution's name is now available as a search term"
    assert world.grounding("nothing known here") == "", \
        "no header announcing nothing"


def test_threads_about_the_same_thing_can_see_each_other():
    """Two live threads on the real stack are about one Detroit house and
    share nothing. Bound to entities, they share the subject."""
    from lifeline import threads as threads_mod
    from lifeline import world

    world.upsert("place", "the Detroit property")
    plumber = threads_mod.create(title="Find a master plumber for the Detroit property")
    meter = threads_mod.create(title="Get a water meter on the Detroit property")

    plumber_about = {e["name"] for e in world.thread_entities(plumber.id)}
    meter_about = {e["name"] for e in world.thread_entities(meter.id)}
    assert "the Detroit property" in plumber_about & meter_about


def test_the_worker_brief_opens_grounded(monkeypatch):
    from lifeline import threads as threads_mod
    from lifeline import world
    from lifeline.assistant import worker

    make_person("nia", "Nia Coleman", relationship="partner")
    world.record_fact("nia", "relation_to_user", "partner")
    thread = threads_mod.create(title="Find pajamas Nia asked for",
                                contact_person_id="nia")

    captured = {}

    def fake_loop(prompt, **kwargs):
        captured["prompt"] = prompt
        return None                      # "no provider" — work() returns early

    monkeypatch.setattr(worker.assistant_loop, "run_loop", fake_loop)
    worker.work(thread.id)

    assert "Nia Coleman" in captured["prompt"]
    assert "relation_to_user=partner" in captured["prompt"]


# ------------------------------------------------ audit F2/F6: identity work

def test_merge_is_an_alias_rewrite_nothing_deleted():
    from lifeline import world as w

    a = w.upsert("person", "Robbbbie Carter")
    make_person("robbbbie-carter", "Robbbbie Carter 2", relationship=None,
                handles=["+19995550177"])
    w.record_fact(a.id, "relation_to_user", "brother")
    w.record_fact("robbbbie-carter", "relation_to_user", "brother")

    assert w.merge_entities(a.id, "robbbbie-carter") is True
    assert w.resolve("Robbbbie Carter").id == "robbbbie-carter", "aliases repointed"
    active = w.facts_for("robbbbie-carter")
    assert [f.value for f in active] == ["brother"], "duplicate facts collapsed"
    row = db.get_connection().execute("select 1 from entities where id=?", (a.id,)).fetchone()
    assert row is not None, "the shell remains; nothing dangles"


def test_relations_pass_through_the_closed_vocabulary(monkeypatch):
    from lifeline import world as w
    from lifeline.extraction import pipeline

    make_conversation("imessage:t1", name="Nia")
    make_person("nia", "Nia", relationship=None)
    message = make_message("anything", person_id="nia", external_id="im-k")
    monkeypatch.setattr("lifeline.extraction.providers.run", lambda fn, kind=None, **kw: {
        "items": [],
        "entities": [
            {"message_id": message.id, "name": "Nia", "kind": "person",
             "claims": [{"predicate": "relation_to_user", "value": "spouse or partner"}]},
            {"message_id": message.id, "name": "Nia", "kind": "person",
             "claims": [{"predicate": "relation_to_user", "value": "visited user's home"}]},
        ],
    })
    pipeline.run(rescore=False)
    values = [f.value for f in w.facts_for("nia") if f.predicate == "relation_to_user"]
    assert values == ["partner"], "canonicalised in; junk dropped"


def test_kinship_comes_from_the_users_own_words():
    from lifeline import threads, world as w

    make_person("milo", "Milo", relationship=None)
    make_person("nia", "Nia", relationship=None)
    make_conversation("imessage:t1", name="Nia")
    message = make_message("Nia is my wife and she's right",
                           is_from_user=True, person_id="nia")
    threads.create(title="Get basketball hoop",
                   summary="I need to get Milo (my son) a basketball hoop.")

    written = w.kinship_backfill(use_llm=False)
    assert written >= 2
    milo = {f.predicate: f.value for f in w.facts_for("milo")}
    assert milo["relation_to_user"] == "son", "from the thread's own words"
    nia_facts = [f for f in w.facts_for("nia") if f.predicate == "relation_to_user"]
    assert nia_facts[0].value == "wife"
    assert nia_facts[0].message_id == message.id, "receipted to his sentence"


def test_mark_self_retires_the_guessed_relations():
    from lifeline import world as w

    make_person("alex", "Alex Carter", relationship=None)
    w.record_fact("alex", "relation_to_user", "friend or acquaintance")
    w.mark_self("alex")
    facts = [f for f in w.facts_for("alex") if f.predicate == "relation_to_user"]
    assert [f.value for f in facts] == ["self"]
    assert facts[0].confidence == 1.0
