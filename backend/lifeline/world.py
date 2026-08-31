"""The world model (§v2.8 phase 2) — the store's standing knowledge.

The pipeline knows *events*: messages, items, threads, each organised around
the moment it arrived. This module holds what those events are *about* —
people, places, organisations, arrangements — accumulated across time, each
fact carrying the message that stated it.

Three rules, inherited from the parts of this codebase that already earned
them:

* **Provenance on every fact.** A fact whose receipt cannot be opened is an
  assertion; the store had forty of those before this existed.
* **Superseded, never deleted.** A child changes school; the old fact is how
  you know when. Same law findings earned in v2.3.
* **Opaque ids for everything new.** `slugify(display_name)` made two Mikes
  one person and made merge a DELETE with dangling pointers. People migrated
  in keep their slugs — seven tables point at them — but nothing new gets one,
  and a future merge is an alias rewrite.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from . import db
from .ingestion.base import normalise_handle
from .models import new_id, now_iso

log = logging.getLogger(__name__)

KINDS = ("person", "place", "org", "arrangement")


@dataclass
class Entity:
    id: str
    kind: str
    name: str


@dataclass
class EntityFact:
    id: str
    entity_id: str
    predicate: str
    value: str
    value_ref: Optional[str] = None
    confidence: float = 0.8
    message_id: Optional[str] = None
    first_seen: str = field(default_factory=now_iso)
    last_seen: str = field(default_factory=now_iso)
    status: str = "active"


def _norm(alias: str) -> str:
    return normalise_handle(alias) if ("@" in alias or any(c.isdigit() for c in alias)) \
        else (alias or "").strip().lower()


# ---------------------------------------------------------------- resolve

def resolve(text: str) -> Optional[Entity]:
    """The entity a name or handle refers to, or None. Exact on the alias
    index — 'Nora', 'nora', '+1 (917) …' and 'nora@…' all land on the same row."""
    alias = _norm(text or "")
    if not alias:
        return None
    row = db.get_connection().execute(
        "SELECT e.id, e.kind, e.name FROM entity_aliases a "
        "JOIN entities e ON e.id = a.entity_id WHERE a.alias = ? LIMIT 1",
        (alias,),
    ).fetchone()
    return Entity(row["id"], row["kind"], row["name"]) if row else None


def mentioned_in(text: str, limit: int = 6) -> List[Entity]:
    """Every entity whose alias appears as a word of `text` — the resolution
    step that runs in front of retrieval (§v2.8 phase 4). Single words match
    per-token; multi-word aliases match as substrings of the lowered text."""
    lowered = (text or "").lower()
    tokens = set(_WORD.findall(lowered))
    # "Nora's daycare" mentions Nora; the possessive is not part of the name.
    tokens |= {t[:-2] for t in tokens if t.endswith("'s")}
    if not tokens:
        return []
    conn = db.get_connection()
    found: dict = {}
    for row in conn.execute(
        "SELECT a.alias, e.id, e.kind, e.name FROM entity_aliases a "
        "JOIN entities e ON e.id = a.entity_id"
    ).fetchall():
        alias = row["alias"]
        hit = alias in tokens if " " not in alias else alias in lowered
        if hit and row["id"] not in found:
            found[row["id"]] = Entity(row["id"], row["kind"], row["name"])
            if len(found) >= limit:
                break
    return list(found.values())


_WORD = __import__("re").compile(r"[a-z0-9']+")


# ------------------------------------------------------------------ write

def upsert(kind: str, name: str, entity_id: Optional[str] = None,
           alias_source: str = "message") -> Entity:
    """The entity this (kind, name) refers to — found through the alias index
    first, created with an opaque id only when nothing answers to the name."""
    existing = resolve(name)
    if existing is not None and (existing.kind == kind or entity_id is None):
        return existing
    conn = db.get_connection()
    eid = entity_id or new_id()
    now = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, kind, name, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (eid, kind, name, now, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, source) VALUES (?,?,?)",
        (eid, _norm(name), alias_source),
    )
    conn.commit()
    return Entity(eid, kind, name)


def add_alias(entity_id: str, alias: str, source: str = "message") -> None:
    normalised = _norm(alias)
    if not normalised:
        return
    conn = db.get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, source) VALUES (?,?,?)",
        (entity_id, normalised, source),
    )
    conn.commit()


def correct_fact(fact_id: str, action: str, value: Optional[str] = None) -> Optional[EntityFact]:
    """The user's word beats the model's (§v2.9). "forget" retires the fact;
    "correct" retires it and writes the user's value at full confidence.
    Superseded, never deleted — the wrong claim stays inspectable."""
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM entity_facts WHERE id = ?", (fact_id,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE entity_facts SET status = 'superseded' WHERE id = ?", (fact_id,))
    conn.commit()
    if action == "correct" and value:
        return record_fact(row["entity_id"], row["predicate"], value, confidence=1.0)
    return EntityFact(**{**{k: row[k] for k in row.keys()}, "status": "superseded"})


# The closed relationship vocabulary (audit F2/F6). Free-vocabulary relations
# produced 43 shapes for one predicate — "visited user's home" filed as a
# *relationship*. Extraction output is canonicalised through this map; what
# cannot be mapped is not a relation and is dropped.
RELATIONS = {
    "self", "wife", "husband", "partner", "son", "daughter", "mother",
    "father", "brother", "sister", "family", "friend", "colleague",
    "client", "service", "teacher", "neighbor", "acquaintance",
}

_RELATION_MAP = {
    "spouse": "partner", "spouse or partner": "partner",
    "spouse or close family member": "partner",
    "wife": "wife", "husband": "husband",
    "girlfriend": "partner", "boyfriend": "partner",
    "child": "family", "child or dependent": "family",
    "child or family member": "family", "kid": "family",
    "mom": "mother", "dad": "father", "mummy": "mother",
    "sibling": "family", "cousin": "family", "relative": "family",
    "friend or acquaintance": "friend", "friend or contact": "friend",
    "friend/colleague": "friend", "close friend": "friend",
    "colleague or team member": "colleague", "coworker": "colleague",
    "co-worker": "colleague", "teammate": "colleague",
    "business contact": "client", "contact": "acquaintance",
    "teacher or instructor": "teacher", "instructor": "teacher",
    "this is the user": "self", "the user": "self", "user": "self", "me": "self",
}


def canonical_relation(value: str) -> Optional[str]:
    """The closed-vocabulary form of a relation claim, or None when the claim
    is not a relationship at all ("visited user's home")."""
    v = (value or "").strip().lower().rstrip(".")
    if v in RELATIONS:
        return v
    if v in _RELATION_MAP:
        return _RELATION_MAP[v]
    # "wife of the user", "his brother" — take the one relation word if there
    # is exactly one. Self-words are excluded from this loose path: "known to
    # user or user's contact" contains "user" twice and mapped a CHILD to
    # "self" on the first live run. Self is only ever an exact, whole claim.
    self_words = {"self", "user", "me"}
    words = [w for w in _WORD.findall(v)
             if (w in RELATIONS or w in _RELATION_MAP) and w not in self_words]
    if len(set(words)) == 1:
        w = words[0]
        return w if w in RELATIONS else _RELATION_MAP[w]
    return None


def merge_entities(source_id: str, target_id: str) -> bool:
    """Two rows, one real-world thing → the target owns everything.

    Merge as an alias rewrite (the promise the schema was built on): the
    source's aliases repoint, its facts move, duplicate facts collapse onto
    the earliest, and the source row remains as an unreachable shell —
    nothing is deleted, so nothing can dangle.
    """
    conn = db.get_connection()
    if source_id == target_id:
        return False
    src = conn.execute("SELECT 1 FROM entities WHERE id=?", (source_id,)).fetchone()
    dst = conn.execute("SELECT 1 FROM entities WHERE id=?", (target_id,)).fetchone()
    if not (src and dst):
        return False
    for row in conn.execute("SELECT alias, source FROM entity_aliases WHERE entity_id=?",
                            (source_id,)).fetchall():
        reassign_alias(row["alias"], target_id, source=row["source"] or "merge")
    conn.execute("UPDATE entity_facts SET entity_id=? WHERE entity_id=?",
                 (target_id, source_id))
    # Collapse duplicates: same predicate+value, keep the earliest first_seen.
    for row in conn.execute(
        """SELECT predicate, LOWER(value) v, COUNT(*) n FROM entity_facts
           WHERE entity_id=? AND status='active' GROUP BY 1,2 HAVING n>1""",
        (target_id,),
    ).fetchall():
        dupes = conn.execute(
            "SELECT id FROM entity_facts WHERE entity_id=? AND predicate=? "
            "AND LOWER(value)=? AND status='active' ORDER BY first_seen",
            (target_id, row["predicate"], row["v"]),
        ).fetchall()
        for extra in dupes[1:]:
            conn.execute("UPDATE entity_facts SET status='superseded' WHERE id=?",
                         (extra["id"],))
    conn.commit()
    log.info("merged entity %s into %s", source_id, target_id)
    return True


def mark_self(entity_id: str) -> None:
    """This entity is the user. One fact, full confidence, and the junk
    relations the model guessed about the user's own row are retired."""
    conn = db.get_connection()
    for row in conn.execute(
        "SELECT id FROM entity_facts WHERE entity_id=? AND predicate='relation_to_user' "
        "AND status='active'", (entity_id,)).fetchall():
        conn.execute("UPDATE entity_facts SET status='superseded' WHERE id=?", (row["id"],))
    conn.commit()
    record_fact(entity_id, "relation_to_user", "self", confidence=1.0)


def reassign_alias(alias: str, entity_id: str, source: str = "merge") -> None:
    """Point an alias at a different entity — the reversible heart of a merge.

    "Merge becomes an alias rewrite, unmerge an alias delete": nothing is
    deleted, no referrer dangles, and resolution flows to the surviving
    entity immediately. The rows the old entity owns stay put until a full
    merge is enacted deliberately.
    """
    normalised = _norm(alias)
    if not normalised:
        return
    conn = db.get_connection()
    conn.execute("DELETE FROM entity_aliases WHERE alias = ?", (normalised,))
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, source) VALUES (?,?,?)",
        (entity_id, normalised, source),
    )
    conn.commit()


def record_fact(
    entity_id: str,
    predicate: str,
    value: str,
    message_id: Optional[str] = None,
    confidence: float = 0.8,
    value_ref: Optional[str] = None,
) -> EntityFact:
    """One claim about the world, with its receipt.

    The same claim again refreshes `last_seen` (and keeps the higher
    confidence); a *different* value for the same predicate supersedes the old
    fact rather than deleting it — the history is the record of when the world
    changed.
    """
    conn = db.get_connection()
    now = now_iso()
    current = conn.execute(
        "SELECT * FROM entity_facts WHERE entity_id = ? AND predicate = ? AND status = 'active'",
        (entity_id, predicate),
    ).fetchall()

    for row in current:
        if row["value"].strip().lower() == (value or "").strip().lower():
            conn.execute(
                "UPDATE entity_facts SET last_seen = ?, confidence = MAX(confidence, ?), "
                "message_id = COALESCE(message_id, ?) WHERE id = ?",
                (now, confidence, message_id, row["id"]),
            )
            conn.commit()
            return EntityFact(
                id=row["id"], entity_id=entity_id, predicate=predicate,
                value=row["value"], value_ref=row["value_ref"],
                confidence=max(row["confidence"], confidence),
                message_id=row["message_id"] or message_id,
                first_seen=row["first_seen"], last_seen=now,
            )

    for row in current:
        conn.execute("UPDATE entity_facts SET status = 'superseded' WHERE id = ?", (row["id"],))

    fact = EntityFact(
        id=new_id(), entity_id=entity_id, predicate=predicate, value=value,
        value_ref=value_ref, confidence=confidence, message_id=message_id,
        first_seen=now, last_seen=now,
    )
    conn.execute(
        "INSERT INTO entity_facts (id, entity_id, predicate, value, value_ref, "
        "confidence, message_id, first_seen, last_seen, status) VALUES (?,?,?,?,?,?,?,?,?,'active')",
        (fact.id, fact.entity_id, fact.predicate, fact.value, fact.value_ref,
         fact.confidence, fact.message_id, fact.first_seen, fact.last_seen),
    )
    conn.commit()
    return fact


def facts_for(entity_id: str, include_superseded: bool = False) -> List[EntityFact]:
    conn = db.get_connection()
    where = "" if include_superseded else " AND status = 'active'"
    rows = conn.execute(
        f"SELECT * FROM entity_facts WHERE entity_id = ?{where} ORDER BY first_seen",
        (entity_id,),
    ).fetchall()
    return [EntityFact(**{k: row[k] for k in row.keys()}) for row in rows]


def mirror_person(person) -> None:
    """Keep a person's entity and aliases current — called from
    `db.upsert_person`, so every path that creates or renames a person keeps
    the world model in step without knowing it exists."""
    conn = db.get_connection()
    now = now_iso()
    conn.execute(
        "INSERT INTO entities (id, kind, name, created_at, updated_at) VALUES (?,'person',?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at",
        (person.id, person.display_name, person.created_at or now, now),
    )
    aliases = {(person.display_name or "").strip().lower()}
    first = (person.display_name or "").strip().split(" ")[0].lower()
    if len(first) > 2 and first.isalpha():
        aliases.add(first)          # people say "Nora"; Contacts says "Nora Carter"
    aliases.update(normalise_handle(h) for h in (person.handles or []))
    for alias in aliases:
        if alias:
            conn.execute(
                "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, source) "
                "VALUES (?,?,'contacts')",
                (person.id, alias),
            )
    conn.commit()


# ------------------------------------------------------------- grounding

def grounding_data(text: str, limit: int = 5) -> List[dict]:
    """The entities a text mentions, with their facts — structured, so the
    ask surface can show what was known (and let the user correct it by fact
    id). `grounding` renders this for the prompt."""
    out = []
    for entity in mentioned_in(text, limit=limit):
        out.append({
            "entity_id": entity.id,
            "name": entity.name,
            "kind": entity.kind,
            "facts": facts_for(entity.id)[:6],
        })
    return out


def grounding(text: str, limit: int = 5) -> str:
    """What the store already knows about the things a question names —
    the resolution step that runs *before* retrieval (§v2.8 phase 4).

    "Where is Nora's daycare?" used to go straight to a keyword search for
    'daycare'. Now the question first resolves Nora, and her facts supply the
    vocabulary the search actually needs — the institution's name, not the
    user's word for it. Empty string when nothing is known: the loop should
    see nothing rather than a header announcing nothing.
    """
    known = grounding_data(text, limit=limit)
    if not known:
        return ""
    lines = ["What is already known about the people and things mentioned:"]
    for entry in known:
        fact_text = "; ".join(f"{f.predicate}={f.value}" for f in entry["facts"]) \
            or "nothing recorded yet"
        lines.append(f"- {entry['name']} ({entry['kind']}): {fact_text}")
    lines.append("Use these names and facts as search terms before concluding "
                 "anything is unknown.")
    return "\n".join(lines)


def bind_thread(thread_id: str, text: str, role: str = "subject",
                person_id: Optional[str] = None) -> int:
    """Attach the entities a thread is about (§v2.8 phase 4). Idempotent —
    the UNIQUE(thread_id, entity_id) constraint makes re-binding free."""
    conn = db.get_connection()
    bound = 0
    targets = list(mentioned_in(text or ""))
    if person_id:
        row = conn.execute("SELECT id, kind, name FROM entities WHERE id = ?",
                           (person_id,)).fetchone()
        if row:
            targets.append(Entity(row["id"], row["kind"], row["name"]))
    for entity in targets:
        cur = conn.execute(
            "INSERT OR IGNORE INTO thread_entities (thread_id, entity_id, role) VALUES (?,?,?)",
            (thread_id, entity.id, "counterparty" if entity.id == person_id else role),
        )
        bound += cur.rowcount
    conn.commit()
    return bound


def thread_entities(thread_id: str) -> List[dict]:
    """The entities a thread is bound to, with their active facts — the shape
    the worker's brief carries."""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT e.id, e.kind, e.name, te.role FROM thread_entities te "
        "JOIN entities e ON e.id = te.entity_id WHERE te.thread_id = ?",
        (thread_id,),
    ).fetchall()
    out = []
    for row in rows:
        out.append({
            "name": row["name"], "kind": row["kind"], "role": row["role"],
            "facts": [f"{f.predicate}={f.value}" for f in facts_for(row["id"])[:6]],
        })
    return out


def search(query: str, limit: int = 6) -> List[dict]:
    """Entities whose name, alias, or facts mention the query — the loop's
    window into standing knowledge. "dentist" finds Brightsmile Pediatric
    Dentistry through its own name, even though no message in the search
    window says the word."""
    term = f"%{(query or '').strip().lower()}%"
    if term == "%%":
        return []
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT DISTINCT e.id, e.kind, e.name FROM entities e
           LEFT JOIN entity_aliases a ON a.entity_id = e.id
           LEFT JOIN entity_facts f ON f.entity_id = e.id AND f.status = 'active'
           WHERE LOWER(e.name) LIKE ? OR a.alias LIKE ?
              OR LOWER(f.value) LIKE ? OR LOWER(f.predicate) LIKE ?
           LIMIT ?""",
        (term, term, term, term, limit),
    ).fetchall()
    out = []
    for row in rows:
        out.append({
            "name": row["name"], "kind": row["kind"],
            "facts": [f"{f.predicate}={f.value}" for f in facts_for(row["id"])[:6]],
        })
    return out


# ---------------------------------------------------------------- kinship

# High-precision shapes only: a name adjacent to a first-person kinship word.
# "Milo (my son)" · "my wife Nia" · "my brother, Robbie". A bare "my son"
# with no name nearby is left for the LLM pass, which must attach a name that
# resolves before anything is written.
_KIN_WORD = "wife|husband|son|daughter|mother|mom|father|dad|brother|sister"
_KIN_PATTERNS = [
    __import__("re").compile(rf"\b(?P<name>[A-Z][a-zA-Z']+)\s*\(\s*my\s+(?P<rel>{_KIN_WORD})\s*\)"),
    __import__("re").compile(rf"\bmy\s+(?P<rel>{_KIN_WORD})[,\s]+(?P<name>[A-Z][a-zA-Z']+)\b"),
    __import__("re").compile(rf"\b(?P<name>[A-Z][a-zA-Z']+)\s+is\s+my\s+(?P<rel>{_KIN_WORD})\b", __import__("re").I),
]


def kinship_backfill(use_llm: bool = True) -> int:
    """Learn the family from the user's own words (audit F2).

    Fact extraction reads what other people write; "my wife" and "my son"
    live in what the USER writes — thread titles, summaries, and outbound
    messages — which no pass ever read. Two stages: regex over the
    high-precision shapes (receipted to the exact message), then one LLM call
    over the harvested kinship sentences for the pronoun-linked cases
    ("She's my wife" two lines under a title naming Nia). Every claim
    must resolve to an existing entity and canonicalise, or it is dropped.
    """
    conn = db.get_connection()
    written = 0
    snippets = []      # (text, message_id_or_None) for the LLM stage

    sources = [(f"{t['title']}\n{t['summary'] or ''}", None) for t in conn.execute(
        "SELECT title, summary FROM threads").fetchall()]
    sources += [(m["text"], m["id"]) for m in conn.execute(
        "SELECT id, text FROM messages WHERE is_from_user = 1 AND ("
        "text LIKE '%my wife%' OR text LIKE '%my husband%' OR text LIKE '%my son%' "
        "OR text LIKE '%my daughter%' OR text LIKE '%my mom%' OR text LIKE '%my mother%' "
        "OR text LIKE '%my dad%' OR text LIKE '%my father%' OR text LIKE '%my brother%' "
        "OR text LIKE '%my sister%' OR text LIKE '%is my %')").fetchall()]

    for text, message_id in sources:
        matched = False
        for pattern in _KIN_PATTERNS:
            for m in pattern.finditer(text or ""):
                relation = canonical_relation(m.group("rel"))
                entity = resolve(m.group("name"))
                if relation and entity:
                    record_fact(entity.id, "relation_to_user", relation,
                                message_id=message_id, confidence=0.95)
                    written += 1
                    matched = True
        if not matched and message_id and len(text or "") < 400:
            snippets.append((text.strip(), message_id))

    if use_llm and snippets:
        written += _kinship_llm(snippets[:40])
    return written


def _kinship_llm(snippets) -> int:
    """One call over the kinship sentences the regexes couldn't attach."""
    import json as _j
    import re as _re

    from .extraction import providers

    allowed = sorted(RELATIONS - {"self"})
    allowed = sorted(RELATIONS - {"self"})
    numbered = "\n".join(f"[{i}] {t}" for i, (t, _) in enumerate(snippets))
    prompt = (
        "These are a user's own messages that mention family. For each, name "
        "the person meant IF the message itself makes it clear (a name in the "
        "sentence). Return JSON {\"kin\": [{\"snippet\": <index>, \"name\": str, "
        f"\"relation\": one of {allowed}}}]}}. "
        "Omit anything uncertain; an empty list is the normal answer.\n\n"
        + numbered
    )
    try:
        raw = providers.run(lambda p: p.complete_json(prompt, max_tokens=400), "kinship")
        if not raw:
            return 0
        match = _re.search(r"\{.*\}", raw, _re.S)
        if not match:
            return 0
        written = 0
        for claim in _j.loads(match.group(0)).get("kin", [])[:12]:
            relation = canonical_relation(str(claim.get("relation") or ""))
            entity = resolve(str(claim.get("name") or ""))
            try:
                idx = int(claim.get("snippet"))
                message_id = snippets[idx][1] if 0 <= idx < len(snippets) else None
            except (TypeError, ValueError):
                message_id = None
            if relation and relation != "self" and entity:
                record_fact(entity.id, "relation_to_user", relation,
                            message_id=message_id, confidence=0.85)
                written += 1
        return written
    except Exception as exc:
        log.warning("kinship llm pass failed: %s", exc)
        return 0
