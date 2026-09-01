"""SQLite store + repository helpers (milestone 1).

Deliberately thin: no ORM, one connection factory, and query helpers that the
engines above share. The same table shapes are mirrored by the on-device store
in the iOS app so sync is a straight column copy.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .config import get_config
log = logging.getLogger(__name__)

from .models import (
    Attachment,
    BehaviorPattern,
    CalendarEvent,
    CompletionSignal,
    Evidence,
    Fact,
    Finding,
    Item,
    Message,
    Person,
    Conversation,
    Thread,
    ThreadState,
    new_id,
    now_iso,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_local = threading.local()


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_config().db_path
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        # The poller (background thread) and API handlers each hold their own
        # connection to the same file. WAL lets readers and one writer coexist,
        # and busy_timeout makes a blocked writer wait instead of instantly
        # raising "database is locked".
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_connection() -> sqlite3.Connection:
    """Per-thread connection, migrated on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect()
        migrate(conn)
        _local.conn = conn
    return conn


def reset_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None


# Ordered migration steps for *existing* databases. Version N of the database
# has applied MIGRATIONS[:N]. schema.sql always describes the latest shape, so
# a fresh database never runs these — it's stamped current immediately.
#
# Each step is a list of SQL statements (ALTER TABLE / CREATE ... IF NOT
# EXISTS), or callables taking the connection when SQL alone can't express the
# condition. Append only; never reorder or edit a shipped step.
def _drop_if_empty(table: str):
    """Remove a table only when it holds no rows — for clearing artifacts a
    partially-applied schema left behind. Never destroys data."""
    def step(conn: sqlite3.Connection) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            return
        if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0:
            conn.execute(f"DROP TABLE {table}")
    return step


def _supersede_backfill(conn: sqlite3.Connection) -> None:
    """Apply the new rule to threads that already have a history.

    Without this the fix only shows on threads worked after the upgrade, and
    every existing thread keeps rendering as the work log the change exists to
    end. Retires all but the newest finding of each kind per thread — the same
    rule `supersede_findings` applies going forward, and equally reversible:
    nothing is deleted, only marked.
    """
    conn.execute(
        """
        UPDATE findings SET superseded_at = ?
        WHERE dismissed_at IS NULL
          AND id NOT IN (
              SELECT id FROM (
                  SELECT id, ROW_NUMBER() OVER (
                      PARTITION BY thread_id, kind ORDER BY created_at DESC, rowid DESC
                  ) AS rn
                  FROM findings WHERE dismissed_at IS NULL
              ) WHERE rn = 1
          )
        """,
        (now_iso(),),
    )


_CITE_MARKUP = re.compile(r"</?cite[^>]*>", re.I)


def _strip_cite_markup(conn: sqlite3.Connection) -> None:
    """Clean citation markup out of stored findings, in place.

    Done in Python rather than SQL because the tags carry varying attributes
    (`<cite index="41-1">`, `<cite index="68-3">`) and SQLite has no regex
    replace. Only touches rows that contain the markup, so it is a no-op on
    every database that never ran a web search.
    """
    rows = conn.execute(
        "SELECT id, headline, body, steps FROM findings "
        "WHERE headline LIKE '%<cite%' OR body LIKE '%<cite%' OR steps LIKE '%<cite%'"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE findings SET headline = ?, body = ?, steps = ? WHERE id = ?",
            (
                _CITE_MARKUP.sub("", row["headline"] or ""),
                _CITE_MARKUP.sub("", row["body"] or ""),
                _CITE_MARKUP.sub("", row["steps"] or "[]"),
                row["id"],
            ),
        )


def _people_become_entities(conn: sqlite3.Connection) -> None:
    """Every person is an entity; every handle and name is an alias."""
    from .ingestion.base import normalise_handle

    now = now_iso()
    for row in conn.execute("SELECT id, display_name, handles, created_at FROM people").fetchall():
        conn.execute(
            "INSERT OR IGNORE INTO entities (id, kind, name, created_at, updated_at) "
            "VALUES (?, 'person', ?, ?, ?)",
            (row["id"], row["display_name"], row["created_at"] or now, now),
        )
        aliases = {(row["display_name"] or "").strip().lower()}
        first = (row["display_name"] or "").strip().split(" ")[0].lower()
        if len(first) > 2 and first.isalpha():
            aliases.add(first)
        for handle in json.loads(row["handles"] or "[]"):
            aliases.add(normalise_handle(handle))
        for alias in aliases:
            if alias:
                conn.execute(
                    "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, source) "
                    "VALUES (?, ?, 'migration')",
                    (row["id"], alias),
                )


def _mail_source_rename_items(conn: sqlite3.Connection) -> None:
    """The half of the rename that was missed.

    Tolerant of a database old enough not to have `items` yet: migrations run
    forward from any version, including ones predating the table."""
    try:
        conn.execute("UPDATE items SET source = 'mail' WHERE source = 'gmail'")
    except sqlite3.OperationalError:
        pass


def _mail_source_rename(conn: sqlite3.Connection) -> None:
    """Rename the mail source in place, conversations and all.

    `conversations.id` is a foreign key target for `messages.conversation_id`,
    so renaming either one alone violates the constraint mid-flight even
    though the pair is consistent by the end. `defer_foreign_keys` holds the
    check until commit, which is exactly the shape this needs; the pragma is
    transaction-scoped and expires on its own.
    """
    conn.execute("PRAGMA defer_foreign_keys = ON")
    statements = [
        "UPDATE conversations SET id = 'mail:' || substr(id, 7) WHERE id LIKE 'gmail:%'",
        "UPDATE conversations SET source = 'mail' WHERE source = 'gmail'",
        "UPDATE messages SET conversation_id = 'mail:' || substr(conversation_id, 7) "
        "WHERE conversation_id LIKE 'gmail:%'",
        "UPDATE messages SET source = 'mail' WHERE source = 'gmail'",
        "UPDATE attachments SET source = 'mail' WHERE source = 'gmail'",
        "UPDATE sync_state SET key = 'applemail:account' WHERE key = 'gmail:account'",
    ]
    for sql in statements:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            # A table this old database never grew. Nothing to rename in it.
            continue


MIGRATIONS: List[List[Any]] = [
    # v1 (§v1.4): the surfacing axis — items are actions or information.
    [
        "ALTER TABLE items ADD COLUMN kind TEXT NOT NULL DEFAULT 'action'",
        "ALTER TABLE items ADD COLUMN category TEXT",
    ],
    # v2 (§v1.5): conversation memory — turns, and the session each loop ran in.
    [
        """CREATE TABLE IF NOT EXISTS conversation_turns (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
            text TEXT NOT NULL, facts TEXT NOT NULL DEFAULT '[]',
            trace TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_turns_session ON conversation_turns(session_id, created_at)",
        "ALTER TABLE loop_runs ADD COLUMN session_id TEXT",
    ],
    # v3 (§v2 step 0): "thread" now means the user's mental loop, so the
    # conversation concept gets its real name back. Mechanical rename only.
    [
        # A live server running schema.sql before this migration can leave an
        # empty `conversations` table behind, which blocks the rename. Clear it
        # if — and only if — it holds nothing.
        _drop_if_empty("conversations"),
        "ALTER TABLE threads RENAME TO conversations",
        "ALTER TABLE messages RENAME COLUMN thread_id TO conversation_id",
        "ALTER TABLE items RENAME COLUMN thread_id TO conversation_id",
        "DROP INDEX IF EXISTS idx_messages_thread",
        "DROP INDEX IF EXISTS idx_items_thread",
        "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_items_conversation ON items(conversation_id)",
    ],
    # v4 (§v2 step 1): threads — the user's open loops — and the evidence they
    # claim. `schema.sql` carries the same CREATEs, so this step exists for the
    # ordering guarantee (walk forward, then fill in) rather than for new SQL;
    # spelled out anyway so the step is readable on its own.
    [
        """CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT 'user', state TEXT NOT NULL DEFAULT 'live',
            key TEXT, deadline TEXT, deadline_source TEXT, deadline_reason TEXT,
            deadline_evidence TEXT NOT NULL DEFAULT '[]',
            importance REAL NOT NULL DEFAULT 0.5, opened_at TEXT NOT NULL,
            resolved_at TEXT, resolved_by TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_threads_state ON threads(state, importance DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_key ON threads(key) WHERE key IS NOT NULL",
        """CREATE TABLE IF NOT EXISTS thread_evidence (
            thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
            kind TEXT NOT NULL DEFAULT 'item', ref_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'claimed', note TEXT, linked_at TEXT NOT NULL,
            PRIMARY KEY (thread_id, kind, ref_id))""",
        "CREATE INDEX IF NOT EXISTS idx_evidence_ref ON thread_evidence(kind, ref_id)",
    ],
    # v5 (§v2 step 2): the lane's "N NEW" badge needs a watermark for when the
    # user last looked at a thread. Without it "new" can only mean "recent",
    # which is a clock's opinion rather than the user's.
    [
        "ALTER TABLE threads ADD COLUMN last_seen_at TEXT",
    ],
    # v6 (§v2 step 4): the worker loop. Findings are what it brings back;
    # `last_worked_at` is how it knows what it already did; `autonomy` is the
    # per-thread ceiling the ladder needs somewhere to live.
    [
        """CREATE TABLE IF NOT EXISTS findings (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
            kind TEXT NOT NULL DEFAULT 'finding',
            headline TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            importance REAL NOT NULL DEFAULT 0.5,
            evidence TEXT NOT NULL DEFAULT '[]',
            loop_run_id TEXT,
            created_at TEXT NOT NULL,
            surfaced_at TEXT,
            dismissed_at TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_findings_thread ON findings(thread_id, created_at DESC)",
        "ALTER TABLE threads ADD COLUMN last_worked_at TEXT",
        "ALTER TABLE threads ADD COLUMN autonomy TEXT NOT NULL DEFAULT 'prepared'",
    ],
    # v7 (§v2 step 5): evidence-based closure. Mirrors `completion_signals` in
    # shape — the user's existing expectation of "closed on its own" vs "asked
    # me" — but keyed on threads, which have none of an item's entity fields.
    [
        """CREATE TABLE IF NOT EXISTS thread_closures (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
            confidence REAL NOT NULL,
            reasons TEXT NOT NULL DEFAULT '[]',
            evidence TEXT NOT NULL DEFAULT '[]',
            evidence_key TEXT NOT NULL DEFAULT '',
            resolution TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            resolved_at TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_closures_thread ON thread_closures(thread_id, resolution)",
    ],
    # v8 (§v2 step 6): watchers — standing monitors a thread implies. Cheap and
    # deterministic; when one fires it attaches evidence, and the worker (which
    # is already evidence-triggered) is what interprets it.
    [
        """CREATE TABLE IF NOT EXISTS watchers (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            spec TEXT NOT NULL DEFAULT '{}',
            what TEXT NOT NULL DEFAULT '',
            cadence_minutes INTEGER NOT NULL DEFAULT 180,
            until TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            last_checked_at TEXT,
            last_fired_at TEXT,
            fire_count INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT 'worker',
            created_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_watchers_thread ON watchers(thread_id, state)",
    ],
    # v9 (§v2 step 7c): findings need their own dedupe key. `item_id` is what
    # `notification_exists_for_item` matches on, and a finding is not an item —
    # without this, "one push per finding, ever" has nothing to hold onto.
    [
        "ALTER TABLE notifications ADD COLUMN finding_id TEXT",
        "CREATE INDEX IF NOT EXISTS idx_notifications_finding ON notifications(finding_id)",
    ],
    # v10: who to contact when this thread needs a message.
    #
    # Deliberately *not* the `person_id` the Thread docstring rules out. That
    # would claim a thread is about a person, which is wrong half the time —
    # "Pay the American Water bill" concerns no one. This is narrower: the
    # counterpart the system would write to on the user's behalf. A thread the
    # user declares themselves has no evidence to infer it from, so before this
    # the writer had to guess a recipient from nothing.
    [
        "ALTER TABLE threads ADD COLUMN contact_person_id TEXT",
        "CREATE INDEX IF NOT EXISTS idx_threads_contact ON threads(contact_person_id)",
    ],
    # v11 (§v2.1 the Initiative Engine): a move is an `action` finding with
    # structure. Not a new table — `action` already means "something prepared
    # for you", already renders, already carries provenance and already flows
    # through the interruption budget. What it lacked was any way to say what
    # KIND of move it is, what was actually staged, and what only the user can
    # supply.
    #
    # `blocked_reason` is the spec's decision 2 in the schema: a move the
    # worker can name but not stage is still worth surfacing, and has to be
    # visibly distinct from one that is ready to act on.
    [
        "ALTER TABLE findings ADD COLUMN move_kind TEXT",       # send|decide|gather|do
        "ALTER TABLE findings ADD COLUMN steps TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE findings ADD COLUMN needs TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE findings ADD COLUMN blocked_reason TEXT",
    ],
    # §v2.3 — findings supersede rather than accumulate.
    #
    # The worker records a finding every pass, and the dedupe compared exact
    # headlines — which never matched, because the headline regenerates with
    # fresh figures ("12 days away", "3 days", "14 hours"). One real thread
    # held six findings of which five were the same observation restated. The
    # thread became a diary and the detail screen rendered all of it.
    [
        "ALTER TABLE findings ADD COLUMN superseded_at TEXT",
    ],
    # Its own step, not an addition to the one above: a database that already
    # applied that ALTER — a running server picks migrations up on reload —
    # would never see a statement appended to it. Append only, never edit.
    [
        _supersede_backfill,
    ],
    # §v2.3 — strip citation markup out of findings already on disk.
    #
    # Web search results carry `<cite index="41-1">...</cite>` and the model
    # quotes it back inside its prose. `registry._plain` stops new findings
    # carrying it, which does nothing for the ones already written: the user
    # opened the app and read `<cite index="41-1">Lifetime 8'-10' portable at
    # $299.99</cite>` on their phone. Right figures, unreadable sentence.
    [
        _strip_cite_markup,
    ],
    # §v2.3 — a finding can carry verified figures, not just prose.
    #
    # `steps` is staged work and belongs to a move, so a pass that researched
    # hard and concluded "no move yet" had nowhere to put what it learned. On
    # the first full day of web search, thirteen worker passes produced zero
    # links: the model's only options were make-a-move or write-an-essay, and
    # the prompt tells it to be conservative about moves.
    [
        "ALTER TABLE findings ADD COLUMN facts TEXT NOT NULL DEFAULT '[]'",
    ],
    # §v2.8 phase 0 — attachments become data. One row per file per message;
    # `text` is what a parser extracted, `error` is why one couldn't, and a
    # failure sets parsed_at so a bad PDF is done rather than retried forever.
    [
        """CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES messages(id),
            source TEXT NOT NULL, remote_id TEXT,
            filename TEXT, mime TEXT, size_bytes INTEGER, sha256 TEXT,
            text TEXT, parsed_at TEXT, error TEXT,
            ingested_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_attachments_identity ON attachments(message_id, sha256)",
    ],
    # §v2.8 phase 1 — the retrieval floor. Ranked, word-boundaried search over
    # messages and their cargo; 'rebuild' indexes everything already stored.
    [
        "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content=messages, content_rowid=rowid)",
        """CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
        END""",
        """CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
        END""",
        """CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF text ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
            INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
        END""",
        "INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')",
        "CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts USING fts5(text, content=attachments, content_rowid=rowid)",
        """CREATE TRIGGER IF NOT EXISTS attachments_fts_ai AFTER INSERT ON attachments BEGIN
            INSERT INTO attachments_fts(rowid, text) VALUES (new.rowid, new.text);
        END""",
        """CREATE TRIGGER IF NOT EXISTS attachments_fts_ad AFTER DELETE ON attachments BEGIN
            INSERT INTO attachments_fts(attachments_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
        END""",
        """CREATE TRIGGER IF NOT EXISTS attachments_fts_au AFTER UPDATE OF text ON attachments BEGIN
            INSERT INTO attachments_fts(attachments_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
            INSERT INTO attachments_fts(rowid, text) VALUES (new.rowid, new.text);
        END""",
        "INSERT INTO attachments_fts(attachments_fts) VALUES ('rebuild')",
    ],
    # §v2.8 phase 2 — the world the events happen in. People become entities
    # under their existing ids (their referrers stay valid); their handles and
    # names become the alias index the resolver reads.
    [
        """CREATE TABLE IF NOT EXISTS entities (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind)",
        """CREATE TABLE IF NOT EXISTS entity_aliases (
            entity_id TEXT NOT NULL REFERENCES entities(id),
            alias TEXT NOT NULL, source TEXT,
            UNIQUE(alias, entity_id))""",
        "CREATE INDEX IF NOT EXISTS idx_aliases_alias ON entity_aliases(alias)",
        """CREATE TABLE IF NOT EXISTS entity_facts (
            id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL REFERENCES entities(id),
            predicate TEXT NOT NULL, value TEXT NOT NULL, value_ref TEXT,
            confidence REAL NOT NULL DEFAULT 0.8,
            message_id TEXT REFERENCES messages(id),
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active')""",
        "CREATE INDEX IF NOT EXISTS idx_efacts_entity ON entity_facts(entity_id, status)",
        """CREATE TABLE IF NOT EXISTS thread_entities (
            thread_id TEXT NOT NULL REFERENCES threads(id),
            entity_id TEXT NOT NULL REFERENCES entities(id),
            role TEXT,
            UNIQUE(thread_id, entity_id))""",
        "CREATE INDEX IF NOT EXISTS idx_tent_entity ON thread_entities(entity_id)",
        _people_become_entities,
    ],
    # §v2.9 — asks become cards with receipts, kept across relaunches.
    [
        """CREATE TABLE IF NOT EXISTS asks (
            id TEXT PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL,
            receipts TEXT NOT NULL DEFAULT '[]', knew TEXT NOT NULL DEFAULT '[]',
            trace TEXT NOT NULL DEFAULT '[]', loop_run_id TEXT,
            created_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_asks_created ON asks(created_at)",
    ],
    # §v3 — the Gmail API door was removed in favour of the local Mail store,
    # so the source is "mail" now: one name for mail however it was read.
    # Rows and conversations keep their identity rather than being orphaned
    # beside new ones covering the same messages.
    [_mail_source_rename],
    # §v3 — nothing authenticates to anyone any more.
    ["DROP TABLE IF EXISTS oauth_tokens"],
    # The mail rename reached messages, conversations and attachments but not
    # `items`, so 163 extracted items still claimed a source their own message
    # no longer had. Anything filtering items by source silently lost them.
    [_mail_source_rename_items],
]


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a database to the current shape.

    Order matters: an existing database is walked *forward first*, then
    schema.sql fills in whatever is brand new. Running schema.sql first would
    describe a shape the old database hasn't reached yet — a rename migration
    hits "no such column" because the new indexes reference the new names.
    """
    fresh = (
        conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] == 0
    )
    if fresh:
        # Brand-new database: schema.sql already is the latest shape.
        conn.executescript(SCHEMA_PATH.read_text())
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
        conn.commit()
        return

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for n, step in enumerate(MIGRATIONS[version:], start=version + 1):
        for sql in step:
            if callable(sql):
                sql(conn)
            else:
                conn.execute(sql)
        conn.execute(f"PRAGMA user_version = {n}")
    conn.commit()
    # Now that the old shape has caught up, add anything newly introduced.
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


@contextmanager
def transaction(conn: Optional[sqlite3.Connection] = None) -> Iterator[sqlite3.Connection]:
    c = conn or get_connection()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


# --------------------------------------------------------------- generic
def _upsert(conn: sqlite3.Connection, table: str, row: Dict[str, Any], key: str = "id") -> None:
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != key)
    sql = (
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[c] for c in cols])


# ---------------------------------------------------------------- people
def upsert_person(person: Person, conn: Optional[sqlite3.Connection] = None) -> Person:
    c = conn or get_connection()
    _upsert(
        c,
        "people",
        {
            "id": person.id,
            "display_name": person.display_name,
            "relationship": person.relationship,
            "handles": json.dumps(person.handles),
            "created_at": person.created_at,
        },
    )
    c.commit()
    # §v2.8 phase 2 — the world model shadows people, so every path that
    # creates or renames one keeps the entity and its aliases in step.
    try:
        from . import world
        world.mirror_person(person)
    except Exception:
        log.exception("entity mirror failed for %s; person kept", person.id)
    return person


def get_person(person_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Person]:
    c = conn or get_connection()
    row = c.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if not row:
        return None
    return Person(
        id=row["id"],
        display_name=row["display_name"],
        relationship=row["relationship"],
        handles=json.loads(row["handles"]),
        created_at=row["created_at"],
    )


def list_people(conn: Optional[sqlite3.Connection] = None) -> List[Person]:
    c = conn or get_connection()
    return [
        Person(
            id=r["id"],
            display_name=r["display_name"],
            relationship=r["relationship"],
            handles=json.loads(r["handles"]),
            created_at=r["created_at"],
        )
        for r in c.execute("SELECT * FROM people ORDER BY display_name")
    ]


def find_person_by_handle(handle: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Person]:
    needle = handle.strip().lower()
    for p in list_people(conn):
        if needle in {h.strip().lower() for h in p.handles} or p.display_name.lower() == needle:
            return p
    return None


# --------------------------------------------------------------- conversations
def upsert_conversation(thread: Conversation, conn: Optional[sqlite3.Connection] = None) -> Conversation:
    c = conn or get_connection()
    _upsert(
        c,
        "conversations",
        {
            "id": thread.id,
            "source": thread.source,
            "display_name": thread.display_name,
            "is_group": int(thread.is_group),
            "created_at": thread.created_at,
        },
    )
    c.commit()
    return thread


def list_conversations(conn: Optional[sqlite3.Connection] = None) -> List[Conversation]:
    c = conn or get_connection()
    return [
        Conversation(
            id=r["id"],
            source=r["source"],
            display_name=r["display_name"],
            is_group=bool(r["is_group"]),
            created_at=r["created_at"],
        )
        for r in c.execute("SELECT * FROM conversations ORDER BY display_name")
    ]


# -------------------------------------------------------------- messages
def insert_messages(messages: Iterable[Message], conn: Optional[sqlite3.Connection] = None) -> int:
    """Idempotent on (source, external_id) so re-importing an export is safe."""
    c = conn or get_connection()
    inserted = 0
    for m in messages:
        cur = c.execute(
            """INSERT OR IGNORE INTO messages
               (id, source, conversation_id, external_id, person_id, is_from_user,
                timestamp, text, metadata, extracted_at, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                m.id,
                m.source,
                m.conversation_id,
                m.external_id,
                m.person_id,
                int(m.is_from_user),
                m.timestamp,
                m.text,
                json.dumps(m.metadata),
                m.extracted_at,
                m.ingested_at,
            ),
        )
        inserted += cur.rowcount
    c.commit()
    return inserted


def update_message_metadata(
    source: str, external_id: str, patch: Dict[str, Any],
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Merge keys into a stored message's metadata.

    Exists because `insert_messages` is INSERT OR IGNORE: a re-poll cannot
    teach an already-stored row anything new, so backfills that discover
    something about old messages (attachment metadata, first) need a way to
    write it without touching the message itself.
    """
    c = conn or get_connection()
    row = c.execute(
        "SELECT id, metadata FROM messages WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    if row is None:
        return False
    metadata = json.loads(row["metadata"] or "{}")
    metadata.update(patch)
    c.execute("UPDATE messages SET metadata = ? WHERE id = ?", (json.dumps(metadata), row["id"]))
    c.commit()
    return True


def save_ask(row: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> None:
    c = conn or get_connection()
    c.execute(
        "INSERT INTO asks (id, question, answer, receipts, knew, trace, loop_run_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (row["id"], row["question"], row["answer"], json.dumps(row.get("receipts", [])),
         json.dumps(row.get("knew", [])), json.dumps(row.get("trace", [])),
         row.get("loop_run_id"), row.get("created_at") or now_iso()),
    )
    c.commit()


def list_asks(limit: int = 20, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    c = conn or get_connection()
    rows = c.execute(
        "SELECT * FROM asks ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "question": r["question"], "answer": r["answer"],
            "receipts": json.loads(r["receipts"] or "[]"),
            "knew": json.loads(r["knew"] or "[]"),
            "trace": json.loads(r["trace"] or "[]"),
            "created_at": r["created_at"],
        })
    return out


def insert_attachment(attachment: "Attachment", conn: Optional[sqlite3.Connection] = None) -> bool:
    """Idempotent on (message_id, sha256): the same file on the same message —
    a re-run of the backfill, a re-polled thread — is one row, not two."""
    c = conn or get_connection()
    cur = c.execute(
        """INSERT OR IGNORE INTO attachments
           (id, message_id, source, remote_id, filename, mime, size_bytes,
            sha256, text, parsed_at, error, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (attachment.id, attachment.message_id, attachment.source, attachment.remote_id,
         attachment.filename, attachment.mime, attachment.size_bytes, attachment.sha256,
         attachment.text, attachment.parsed_at, attachment.error, attachment.ingested_at),
    )
    c.commit()
    return cur.rowcount > 0


def attachments_for_message(message_id: str, conn: Optional[sqlite3.Connection] = None) -> List["Attachment"]:
    c = conn or get_connection()
    rows = c.execute(
        "SELECT * FROM attachments WHERE message_id = ? ORDER BY ingested_at, filename",
        (message_id,),
    ).fetchall()
    return [Attachment(**dict(r)) for r in rows]


def attachment_text_by_sha(sha256: str, conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    """The parse of this exact content, if any copy of it was parsed before.
    The preschool packet arrives three times; the work happens once."""
    c = conn or get_connection()
    row = c.execute(
        "SELECT text FROM attachments WHERE sha256 = ? AND parsed_at IS NOT NULL "
        "AND text IS NOT NULL LIMIT 1",
        (sha256,),
    ).fetchone()
    return row["text"] if row else None


def get_message_by_external_id(
    source: str, external_id: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[Message]:
    c = conn or get_connection()
    row = c.execute(
        "SELECT * FROM messages WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    return _message_from_row(row) if row else None


def _message_from_row(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        source=row["source"],
        conversation_id=row["conversation_id"],
        external_id=row["external_id"],
        person_id=row["person_id"],
        is_from_user=bool(row["is_from_user"]),
        timestamp=row["timestamp"],
        text=row["text"],
        metadata=json.loads(row["metadata"]),
        extracted_at=row["extracted_at"],
        ingested_at=row["ingested_at"],
    )


def unextracted_messages(limit: int = 200, conn: Optional[sqlite3.Connection] = None) -> List[Message]:
    """§5: re-extraction runs on new messages only."""
    c = conn or get_connection()
    rows = c.execute(
        "SELECT * FROM messages WHERE extracted_at IS NULL ORDER BY timestamp LIMIT ?", (limit,)
    ).fetchall()
    return [_message_from_row(r) for r in rows]


def mark_extracted(message_ids: Iterable[str], conn: Optional[sqlite3.Connection] = None) -> None:
    c = conn or get_connection()
    ts = now_iso()
    c.executemany("UPDATE messages SET extracted_at = ? WHERE id = ?", [(ts, mid) for mid in message_ids])
    c.commit()


def thread_messages(conversation_id: str, conn: Optional[sqlite3.Connection] = None) -> List[Message]:
    c = conn or get_connection()
    rows = c.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY timestamp", (conversation_id,)).fetchall()
    return [_message_from_row(r) for r in rows]


def messages_since(iso_ts: str, source: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> List[Message]:
    c = conn or get_connection()
    if source:
        rows = c.execute(
            "SELECT * FROM messages WHERE timestamp >= ? AND source = ? ORDER BY timestamp", (iso_ts, source)
        ).fetchall()
    else:
        rows = c.execute("SELECT * FROM messages WHERE timestamp >= ? ORDER BY timestamp", (iso_ts,)).fetchall()
    return [_message_from_row(r) for r in rows]


def get_message(message_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Message]:
    c = conn or get_connection()
    row = c.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return _message_from_row(row) if row else None


def user_reply_after(
    conversation_id: str, iso_ts: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[Message]:
    """The user's first own message in this thread *after* a moment — strong
    evidence that whatever was asked at that moment has been addressed. The
    backbone of the currency signal and iMessage self-reply auto-close."""
    c = conn or get_connection()
    row = c.execute(
        "SELECT * FROM messages WHERE conversation_id = ? AND timestamp > ? AND is_from_user = 1 "
        "ORDER BY timestamp ASC LIMIT 1",
        (conversation_id, iso_ts),
    ).fetchone()
    return _message_from_row(row) if row else None


def user_replies_after(
    conversation_id: str, iso_ts: str, limit: int = 10,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Message]:
    """The user's own messages after a moment, oldest first.

    `user_reply_after` answers "did they say anything at all", which is the
    right question for the currency signal. Thread closure asks a narrower one
    — did they say anything *about this* — and the first reply after a thread
    opens is very often "ok" to something else entirely, so it has to be able
    to read past it.
    """
    c = conn or get_connection()
    rows = c.execute(
        "SELECT * FROM messages WHERE conversation_id = ? AND timestamp > ? AND is_from_user = 1 "
        "ORDER BY timestamp ASC LIMIT ?",
        (conversation_id, iso_ts, limit),
    ).fetchall()
    return [_message_from_row(r) for r in rows]


def last_user_message(conversation_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Message]:
    """Your most recent message in a thread — 'where you left off', the memory-jog."""
    c = conn or get_connection()
    row = c.execute(
        "SELECT * FROM messages WHERE conversation_id = ? AND is_from_user = 1 "
        "ORDER BY timestamp DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return _message_from_row(row) if row else None


def last_message(conversation_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Message]:
    """The most recent message in a thread, either direction — if it's yours, the
    thread is awaiting a reply from them (silence you may have forgotten)."""
    c = conn or get_connection()
    row = c.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY timestamp DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return _message_from_row(row) if row else None


def message_context(
    conversation_id: str,
    pivot_ts: str,
    before: int = 4,
    after: int = 2,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Message]:
    """The few messages surrounding a pivot moment in a thread, in chronological
    order — enough back-and-forth to remember what was being discussed. Two
    bounded, index-backed queries so this stays cheap on 800-message threads."""
    c = conn or get_connection()
    pre = c.execute(
        "SELECT * FROM messages WHERE conversation_id = ? AND timestamp <= ? "
        "ORDER BY timestamp DESC LIMIT ?",
        (conversation_id, pivot_ts, before + 1),
    ).fetchall()
    post = c.execute(
        "SELECT * FROM messages WHERE conversation_id = ? AND timestamp > ? "
        "ORDER BY timestamp ASC LIMIT ?",
        (conversation_id, pivot_ts, after),
    ).fetchall()
    rows = list(reversed(pre)) + list(post)
    return [_message_from_row(r) for r in rows]


# ------------------------------------------------------- calendar events
def upsert_calendar_events(events: Iterable[CalendarEvent], conn: Optional[sqlite3.Connection] = None) -> int:
    c = conn or get_connection()
    n = 0
    for e in events:
        _upsert(
            c,
            "calendar_events",
            {
                "id": e.id,
                "calendar_id": e.calendar_id,
                "summary": e.summary,
                "description": e.description,
                "location": e.location,
                "start_at": e.start_at,
                "end_at": e.end_at,
                "status": e.status,
                "attendees": json.dumps(e.attendees),
                "self_response": e.self_response,
                "updated_at": e.updated_at,
                "ingested_at": e.ingested_at,
            },
        )
        n += 1
    c.commit()
    return n


def _calendar_from_row(row: sqlite3.Row) -> CalendarEvent:
    return CalendarEvent(
        id=row["id"],
        calendar_id=row["calendar_id"],
        summary=row["summary"],
        description=row["description"],
        location=row["location"],
        start_at=row["start_at"],
        end_at=row["end_at"],
        status=row["status"],
        attendees=json.loads(row["attendees"]),
        self_response=row["self_response"],
        updated_at=row["updated_at"],
        ingested_at=row["ingested_at"],
    )


def list_calendar_events(conn: Optional[sqlite3.Connection] = None) -> List[CalendarEvent]:
    c = conn or get_connection()
    return [_calendar_from_row(r) for r in c.execute("SELECT * FROM calendar_events ORDER BY start_at")]


# ----------------------------------------------------------------- items
def save_item(item: Item, conn: Optional[sqlite3.Connection] = None) -> Item:
    c = conn or get_connection()
    item.updated_at = now_iso()
    _upsert(c, "items", item.to_row())
    c.commit()
    return item


def save_items(items: Iterable[Item], conn: Optional[sqlite3.Connection] = None) -> int:
    c = conn or get_connection()
    n = 0
    ts = now_iso()
    for item in items:
        item.updated_at = ts
        _upsert(c, "items", item.to_row())
        n += 1
    c.commit()
    return n


def get_item(item_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Item]:
    c = conn or get_connection()
    row = c.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return Item.from_row(row) if row else None


def list_items(
    status: Optional[str] = None,
    conversation_id: Optional[str] = None,
    person_id: Optional[str] = None,
    updated_since: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Item]:
    c = conn or get_connection()
    clauses: List[str] = []
    params: List[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if conversation_id:
        clauses.append("conversation_id = ?")
        params.append(conversation_id)
    if person_id:
        clauses.append("person_id = ?")
        params.append(person_id)
    if updated_since:
        clauses.append("updated_at > ?")
        params.append(updated_since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = c.execute(f"SELECT * FROM items {where} ORDER BY score DESC, timestamp DESC", params).fetchall()
    return [Item.from_row(r) for r in rows]


def open_items(conn: Optional[sqlite3.Connection] = None) -> List[Item]:
    """Pending + snoozed: everything the engines still care about."""
    c = conn or get_connection()
    rows = c.execute(
        "SELECT * FROM items WHERE status IN ('pending','snoozed') ORDER BY score DESC"
    ).fetchall()
    return [Item.from_row(r) for r in rows]


def find_item_by_message(message_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Item]:
    c = conn or get_connection()
    row = c.execute("SELECT * FROM items WHERE message_id = ? LIMIT 1", (message_id,)).fetchone()
    return Item.from_row(row) if row else None


# --------------------------------------------------------------- threads
def save_thread(thread: Thread, conn: Optional[sqlite3.Connection] = None) -> Thread:
    c = conn or get_connection()
    thread.updated_at = now_iso()
    _upsert(c, "threads", thread.to_row())
    c.commit()
    return thread


def get_thread(thread_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Thread]:
    c = conn or get_connection()
    row = c.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return Thread.from_row(row) if row else None


def get_thread_by_key(key: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Thread]:
    c = conn or get_connection()
    row = c.execute("SELECT * FROM threads WHERE key = ?", (key,)).fetchone()
    return Thread.from_row(row) if row else None


def list_threads(
    states: Optional[Iterable[str]] = None,
    key_prefix: Optional[str] = None,
    reference: Optional[Any] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Thread]:
    """Threads, most pressing first.

    An *upcoming* deadline outranks importance, because a date is a fact about
    the world and importance is only a belief about it. A deadline that has
    already passed does not: it sorts below undated threads, matching
    `signals.deadline_pressure`, which scores a gone date at -0.4 ("date passed
    N days ago") rather than treating it as urgency.

    That ordering was inverted in the first cut of this table, and it showed up
    immediately on real data: a skating trip whose date was three months gone
    sorted ahead of a bill due next week and held the top of the stack
    permanently. On a product that promises *fewer* threads, the number-one
    slot is the last place a dead date should live. The deadline itself is
    kept — it is true, and the UI can strike it through — only its claim on
    the top of the stack is withdrawn.

    Sorted in Python rather than SQL because ISO-8601 strings only compare
    correctly when they share a UTC offset, and a user-set deadline need not.
    """
    from .models import parse_iso

    c = conn or get_connection()
    clauses: List[str] = []
    params: List[Any] = []
    if states is not None:
        states = list(states)
        if not states:
            return []
        clauses.append(f"state IN ({','.join('?' for _ in states)})")
        params.extend(states)
    if key_prefix:
        clauses.append("key LIKE ?")
        params.append(f"{key_prefix}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    threads = [Thread.from_row(r) for r in c.execute(f"SELECT * FROM threads {where}", params)]

    from datetime import datetime, timezone

    now = reference or datetime.now(timezone.utc)

    def rank(t: Thread):
        due = parse_iso(t.deadline)
        if due is None:
            band = 1                      # no date: ranked on importance alone
        elif due >= now:
            band = 0                      # upcoming: the strongest claim there is
        else:
            band = 2                      # passed: real, but no longer urgent
        return (band, due or now, -t.importance, t.opened_at)

    # Passed deadlines read most-recent-first — a date gone two days is more
    # live than one gone three months.
    upcoming = sorted([t for t in threads if rank(t)[0] == 0], key=rank)
    undated = sorted([t for t in threads if rank(t)[0] == 1], key=lambda t: (-t.importance, t.opened_at))
    passed = sorted(
        [t for t in threads if rank(t)[0] == 2],
        key=lambda t: (parse_iso(t.deadline), -t.importance),
        reverse=True,
    )
    return upcoming + undated + passed


def open_threads(conn: Optional[sqlite3.Connection] = None) -> List[Thread]:
    """The main stack: live + quiet. Proposals are deliberately excluded —
    nothing reaches the stack the user hasn't acknowledged carrying."""
    return list_threads(states=ThreadState.OPEN, conn=conn)


def add_evidence(evidence: Evidence, conn: Optional[sqlite3.Connection] = None) -> Evidence:
    """Idempotent: claiming the same row twice keeps the first link (and its
    role, so a re-claim can't demote the founding evidence to 'claimed')."""
    c = conn or get_connection()
    c.execute(
        "INSERT OR IGNORE INTO thread_evidence (thread_id, kind, ref_id, role, note, linked_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            evidence.thread_id,
            evidence.kind,
            evidence.ref_id,
            evidence.role,
            evidence.note,
            evidence.linked_at,
        ),
    )
    c.commit()
    return evidence


def remove_evidence(
    thread_id: str, kind: str, ref_id: str, conn: Optional[sqlite3.Connection] = None
) -> bool:
    c = conn or get_connection()
    cur = c.execute(
        "DELETE FROM thread_evidence WHERE thread_id = ? AND kind = ? AND ref_id = ?",
        (thread_id, kind, ref_id),
    )
    c.commit()
    return cur.rowcount > 0


def _evidence_from_row(row: sqlite3.Row) -> Evidence:
    return Evidence(
        thread_id=row["thread_id"],
        kind=row["kind"],
        ref_id=row["ref_id"],
        role=row["role"],
        note=row["note"],
        linked_at=row["linked_at"],
    )


def thread_evidence(thread_id: str, conn: Optional[sqlite3.Connection] = None) -> List[Evidence]:
    c = conn or get_connection()
    rows = c.execute(
        "SELECT * FROM thread_evidence WHERE thread_id = ? ORDER BY linked_at", (thread_id,)
    ).fetchall()
    return [_evidence_from_row(r) for r in rows]


def threads_claiming(kind: str, ref_id: str, conn: Optional[sqlite3.Connection] = None) -> List[Thread]:
    """Which threads already claim this row — the 'does it fit a live thread?'
    question, asked from the evidence side."""
    c = conn or get_connection()
    rows = c.execute(
        "SELECT t.* FROM threads t JOIN thread_evidence e ON e.thread_id = t.id "
        "WHERE e.kind = ? AND e.ref_id = ?",
        (kind, ref_id),
    ).fetchall()
    return [Thread.from_row(r) for r in rows]


# -------------------------------------------------------------- watchers
def save_watcher(watcher: Any, conn: Optional[sqlite3.Connection] = None) -> Any:
    c = conn or get_connection()
    _upsert(c, "watchers", watcher.to_row())
    c.commit()
    return watcher


def get_watcher(watcher_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Any]:
    from .threads.watchers import Watcher

    c = conn or get_connection()
    row = c.execute("SELECT * FROM watchers WHERE id = ?", (watcher_id,)).fetchone()
    return Watcher.from_row(row) if row else None


def thread_watchers(
    thread_id: str, include_expired: bool = False, conn: Optional[sqlite3.Connection] = None
) -> List[Any]:
    from .threads.watchers import Watcher

    c = conn or get_connection()
    where = "WHERE thread_id = ?" + ("" if include_expired else " AND state = 'active'")
    rows = c.execute(f"SELECT * FROM watchers {where} ORDER BY created_at", (thread_id,)).fetchall()
    return [Watcher.from_row(r) for r in rows]


def active_watchers(conn: Optional[sqlite3.Connection] = None) -> List[Any]:
    from .threads.watchers import Watcher

    c = conn or get_connection()
    rows = c.execute("SELECT * FROM watchers WHERE state = 'active'").fetchall()
    return [Watcher.from_row(r) for r in rows]


def delete_watcher(watcher_id: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    c = conn or get_connection()
    cur = c.execute("DELETE FROM watchers WHERE id = ?", (watcher_id,))
    c.commit()
    return cur.rowcount > 0


# ------------------------------------------------------- thread closures
def save_thread_closure(
    thread_id: str,
    confidence: float,
    reasons: List[str],
    evidence: List[Dict[str, str]],
    resolution: str,
    evidence_key: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    c = conn or get_connection()
    record_id = new_id()
    key = evidence_key if evidence_key is not None else _evidence_key(evidence)
    c.execute(
        """INSERT INTO thread_closures
           (id, thread_id, confidence, reasons, evidence, evidence_key, resolution, detected_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (record_id, thread_id, confidence, json.dumps(reasons), json.dumps(evidence),
         key, resolution, now_iso()),
    )
    c.commit()
    return {"id": record_id, "thread_id": thread_id, "resolution": resolution}


def _evidence_key(evidence: List[Dict[str, str]]) -> str:
    """A stable fingerprint of what the case rests on, so the same argument is
    never put to the user twice.

    Stable only for arguments made of fixed things. An argument that rests on
    "the user replied" changes key every time the user replies again, so a
    rejected closure came back the next time they typed anything — the hoop
    thread was put to the user twice in one day. Callers whose argument has a
    *shape* rather than a set of rows pass their own key; see
    `threads.closure.ThreadMatch.argument_key`.
    """
    return "|".join(sorted(f"{e.get('kind')}:{e.get('ref_id')}" for e in evidence))


def thread_closure_exists(
    thread_id: str, evidence_key: str, conn: Optional[sqlite3.Connection] = None
) -> bool:
    c = conn or get_connection()
    row = c.execute(
        "SELECT 1 FROM thread_closures WHERE thread_id = ? AND evidence_key = ? LIMIT 1",
        (thread_id, evidence_key),
    ).fetchone()
    return row is not None


def get_thread_closure(
    closure_id: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[Dict[str, Any]]:
    c = conn or get_connection()
    row = c.execute("SELECT * FROM thread_closures WHERE id = ?", (closure_id,)).fetchone()
    return _closure_from_row(row) if row else None


def _closure_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["reasons"] = json.loads(d.get("reasons") or "[]")
    d["evidence"] = json.loads(d.get("evidence") or "[]")
    return d


def pending_thread_closures(conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    c = conn or get_connection()
    rows = c.execute(
        "SELECT * FROM thread_closures WHERE resolution = 'needs_confirmation' "
        "ORDER BY confidence DESC"
    ).fetchall()
    return [_closure_from_row(r) for r in rows]


def resolve_thread_closure(
    closure_id: str, resolution: str, conn: Optional[sqlite3.Connection] = None
) -> None:
    c = conn or get_connection()
    c.execute(
        "UPDATE thread_closures SET resolution = ?, resolved_at = ? WHERE id = ?",
        (resolution, now_iso(), closure_id),
    )
    c.commit()


# -------------------------------------------------------------- findings
def save_finding(finding: Finding, conn: Optional[sqlite3.Connection] = None) -> Finding:
    c = conn or get_connection()
    _upsert(c, "findings", finding.to_row())
    c.commit()
    return finding


def get_finding(finding_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Finding]:
    c = conn or get_connection()
    row = c.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    return Finding.from_row(row) if row else None


def thread_findings(
    thread_id: str,
    include_dismissed: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Finding]:
    c = conn or get_connection()
    where = "WHERE thread_id = ?" + ("" if include_dismissed else " AND dismissed_at IS NULL")
    rows = c.execute(
        f"SELECT * FROM findings {where} ORDER BY created_at DESC", (thread_id,)
    ).fetchall()
    return [Finding.from_row(r) for r in rows]


def _headline_shape(headline: str) -> str:
    """A headline with its moving parts removed.

    Exact matching never fired, because the restatement is never identical:
    "deadline is 12 days away", then "3 days", then "14 hours". Every number
    and date is the part that changes while the observation stays the same, so
    the comparison drops them and keeps the words.
    """
    text = re.sub(r"\d+", "#", (headline or "").lower())
    return " ".join(re.sub(r"[^a-z#\s]", " ", text).split())


def finding_exists(thread_id: str, headline: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    """The worker runs on a schedule, so the same observation would otherwise
    be recorded every pass. A thread that says the same thing five times is
    noise wearing the costume of diligence.

    Compares shapes, not strings, and only against findings that are still
    current: an observation that was superseded a week ago is allowed to
    recur, because by then it is news again.
    """
    c = conn or get_connection()
    shape = _headline_shape(headline)
    rows = c.execute(
        "SELECT headline FROM findings "
        "WHERE thread_id = ? AND dismissed_at IS NULL AND superseded_at IS NULL",
        (thread_id,),
    ).fetchall()
    return any(_headline_shape(r["headline"]) == shape for r in rows)


def supersede_findings(thread_id: str, kind: str, keep_id: str,
                       conn: Optional[sqlite3.Connection] = None) -> int:
    """Retire the thread's older findings of this kind, keeping `keep_id`.

    The current picture is the newest of each kind: one move, one observation,
    one "I looked and found nothing". Everything before it is history — still
    readable, no longer competing for the screen.

    The trade is deliberate: a still-relevant older observation of the same
    kind drops out of the current view. That is the right cost, because the
    newest pass is the one that read the whole thread before writing, and the
    alternative is what shipped — six findings of equal weight, five of them
    the same sentence with a fresher number.
    """
    c = conn or get_connection()
    cur = c.execute(
        "UPDATE findings SET superseded_at = ? "
        "WHERE thread_id = ? AND kind = ? AND id != ? "
        "AND dismissed_at IS NULL AND superseded_at IS NULL",
        (now_iso(), thread_id, kind, keep_id),
    )
    c.commit()
    return cur.rowcount


# ------------------------------------------------------- behavior events
def log_behavior(
    kind: str,
    item_id: Optional[str] = None,
    person_id: Optional[str] = None,
    item_type: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    occurred_at: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    c = conn or get_connection()
    if item_id and (person_id is None or item_type is None):
        row = c.execute("SELECT person_id, type FROM items WHERE id = ?", (item_id,)).fetchone()
        if row:
            person_id = person_id or row["person_id"]
            item_type = item_type or row["type"]
    c.execute(
        """INSERT INTO behavior_events (item_id, person_id, item_type, kind, occurred_at, payload)
           VALUES (?,?,?,?,?,?)""",
        (item_id, person_id, item_type, kind, occurred_at or now_iso(), json.dumps(payload or {})),
    )
    c.commit()


def behavior_for_item(item_id: str, conn: Optional[sqlite3.Connection] = None) -> List[sqlite3.Row]:
    c = conn or get_connection()
    return c.execute(
        "SELECT * FROM behavior_events WHERE item_id = ? ORDER BY occurred_at", (item_id,)
    ).fetchall()


def behavior_counts(item_id: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, int]:
    c = conn or get_connection()
    rows = c.execute(
        "SELECT kind, COUNT(*) AS n FROM behavior_events WHERE item_id = ? GROUP BY kind", (item_id,)
    ).fetchall()
    return {r["kind"]: r["n"] for r in rows}


def behavior_events(
    kinds: Optional[Iterable[str]] = None,
    person_id: Optional[str] = None,
    item_type: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[sqlite3.Row]:
    c = conn or get_connection()
    clauses: List[str] = []
    params: List[Any] = []
    if kinds:
        kinds = list(kinds)
        clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
        params.extend(kinds)
    if person_id:
        clauses.append("person_id = ?")
        params.append(person_id)
    if item_type:
        clauses.append("item_type = ?")
        params.append(item_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return c.execute(f"SELECT * FROM behavior_events {where} ORDER BY occurred_at", params).fetchall()


# ---------------------------------------------------- completion signals
def save_signal(sig: CompletionSignal, conn: Optional[sqlite3.Connection] = None) -> CompletionSignal:
    c = conn or get_connection()
    _upsert(
        c,
        "completion_signals",
        {
            "id": sig.id,
            "item_id": sig.item_id,
            "source": sig.source,
            "evidence_ref": sig.evidence_ref,
            "evidence_text": sig.evidence_text,
            "confidence": sig.confidence,
            "reasons": json.dumps(sig.reasons),
            "resolution": sig.resolution,
            "detected_at": sig.detected_at,
            "resolved_at": sig.resolved_at,
        },
    )
    c.commit()
    return sig


def _signal_from_row(row: sqlite3.Row) -> CompletionSignal:
    return CompletionSignal(
        id=row["id"],
        item_id=row["item_id"],
        source=row["source"],
        evidence_ref=row["evidence_ref"],
        evidence_text=row["evidence_text"],
        confidence=row["confidence"],
        reasons=json.loads(row["reasons"]),
        resolution=row["resolution"],
        detected_at=row["detected_at"],
        resolved_at=row["resolved_at"],
    )


def signals_for_item(item_id: str, conn: Optional[sqlite3.Connection] = None) -> List[CompletionSignal]:
    c = conn or get_connection()
    rows = c.execute(
        "SELECT * FROM completion_signals WHERE item_id = ? ORDER BY detected_at DESC", (item_id,)
    ).fetchall()
    return [_signal_from_row(r) for r in rows]


def pending_confirmations(conn: Optional[sqlite3.Connection] = None) -> List[CompletionSignal]:
    c = conn or get_connection()
    rows = c.execute(
        "SELECT * FROM completion_signals WHERE resolution = 'needs_confirmation' ORDER BY confidence DESC"
    ).fetchall()
    return [_signal_from_row(r) for r in rows]


def get_signal(signal_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[CompletionSignal]:
    c = conn or get_connection()
    row = c.execute("SELECT * FROM completion_signals WHERE id = ?", (signal_id,)).fetchone()
    return _signal_from_row(row) if row else None


def signal_exists(item_id: str, evidence_ref: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    c = conn or get_connection()
    row = c.execute(
        "SELECT 1 FROM completion_signals WHERE item_id = ? AND evidence_ref = ? LIMIT 1",
        (item_id, evidence_ref),
    ).fetchone()
    return row is not None


# --------------------------------------------------------------- weights
def get_weight(key: str, default: float = 0.0, conn: Optional[sqlite3.Connection] = None) -> float:
    c = conn or get_connection()
    row = c.execute("SELECT value FROM model_weights WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_weight_row(key: str, conn: Optional[sqlite3.Connection] = None) -> Optional[sqlite3.Row]:
    c = conn or get_connection()
    return c.execute("SELECT * FROM model_weights WHERE key = ?", (key,)).fetchone()


def set_weight(key: str, value: float, observations: Optional[int] = None, conn: Optional[sqlite3.Connection] = None) -> None:
    c = conn or get_connection()
    if observations is None:
        row = get_weight_row(key, conn=c)
        observations = (row["observations"] if row else 0) + 1
    _upsert(
        c,
        "model_weights",
        {"key": key, "value": value, "observations": observations, "updated_at": now_iso()},
        key="key",
    )
    c.commit()


def all_weights(prefix: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Dict[str, float]:
    c = conn or get_connection()
    if prefix:
        rows = c.execute("SELECT key, value FROM model_weights WHERE key LIKE ?", (f"{prefix}%",)).fetchall()
    else:
        rows = c.execute("SELECT key, value FROM model_weights").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ------------------------------------------------------------ sync state
def get_sync_state(key: str, default: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    c = conn or get_connection()
    row = c.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_sync_state(key: str, value: str, conn: Optional[sqlite3.Connection] = None) -> None:
    c = conn or get_connection()
    _upsert(c, "sync_state", {"key": key, "value": value, "updated_at": now_iso()}, key="key")
    c.commit()


# -------------------------------------------------------- notifications
def queue_notification(
    kind: str,
    interruption: str,
    title: str,
    body: str,
    item_id: Optional[str] = None,
    finding_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    c = conn or get_connection()
    nid = new_id()
    c.execute(
        """INSERT INTO notifications
           (id, item_id, finding_id, kind, interruption, title, body, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (nid, item_id, finding_id, kind, interruption, title, body, now_iso()),
    )
    c.commit()
    return nid


def notification_exists_for_finding(
    finding_id: str, conn: Optional[sqlite3.Connection] = None
) -> bool:
    """One push per finding, ever — the finding-shaped twin of
    `notification_exists_for_item`."""
    c = conn or get_connection()
    row = c.execute(
        "SELECT 1 FROM notifications WHERE finding_id = ? LIMIT 1", (finding_id,)
    ).fetchone()
    return row is not None


def unsurfaced_findings(conn: Optional[sqlite3.Connection] = None) -> List[Finding]:
    """Findings that have never reached the user, newest first."""
    c = conn or get_connection()
    rows = c.execute(
        "SELECT * FROM findings WHERE surfaced_at IS NULL AND dismissed_at IS NULL "
        "AND kind != 'nothing' ORDER BY importance DESC, created_at DESC"
    ).fetchall()
    return [Finding.from_row(r) for r in rows]


def dismiss_finding(finding_id: str, conn: Optional[sqlite3.Connection] = None) -> None:
    """Take a finding off the thread. Kept rather than deleted — a rejected
    move is evidence about the user, and the row is what `move_rejected` in
    behavior_events points at."""
    c = conn or get_connection()
    c.execute("UPDATE findings SET dismissed_at = ? WHERE id = ?", (now_iso(), finding_id))
    c.commit()


def mark_finding_surfaced(finding_id: str, conn: Optional[sqlite3.Connection] = None) -> None:
    c = conn or get_connection()
    c.execute("UPDATE findings SET surfaced_at = ? WHERE id = ?", (now_iso(), finding_id))
    c.commit()


def unsent_notifications(conn: Optional[sqlite3.Connection] = None) -> List[sqlite3.Row]:
    c = conn or get_connection()
    return c.execute("SELECT * FROM notifications WHERE sent_at IS NULL ORDER BY created_at").fetchall()


def mark_notification_sent(nid: str, conn: Optional[sqlite3.Connection] = None) -> None:
    c = conn or get_connection()
    c.execute("UPDATE notifications SET sent_at = ? WHERE id = ?", (now_iso(), nid))
    c.commit()


def notification_exists_for_item(item_id: str, kind: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    c = conn or get_connection()
    row = c.execute(
        "SELECT 1 FROM notifications WHERE item_id = ? AND kind = ? LIMIT 1", (item_id, kind)
    ).fetchone()
    return row is not None


def register_device(token: str, platform: str = "ios", conn: Optional[sqlite3.Connection] = None) -> None:
    c = conn or get_connection()
    _upsert(c, "devices", {"token": token, "platform": platform, "registered_at": now_iso()}, key="token")
    c.commit()


def list_devices(conn: Optional[sqlite3.Connection] = None) -> List[str]:
    c = conn or get_connection()
    return [r["token"] for r in c.execute("SELECT token FROM devices")]


# ----------------------------------------------------------------- facts
def upsert_fact(fact: Fact, conn: Optional[sqlite3.Connection] = None) -> Fact:
    c = conn or get_connection()
    fact.updated_at = now_iso()
    _upsert(
        c,
        "facts",
        {
            "id": fact.id,
            "subject_type": fact.subject_type,
            "subject_id": fact.subject_id,
            "statement": fact.statement,
            "predicate": fact.predicate,
            "value": fact.value,
            "source": fact.source,
            "confidence": fact.confidence,
            "provenance": fact.provenance,
            "status": fact.status,
            "created_at": fact.created_at,
            "updated_at": fact.updated_at,
        },
    )
    c.commit()
    return fact


def _fact_from_row(row: sqlite3.Row) -> Fact:
    return Fact(**{k: row[k] for k in Fact.__dataclass_fields__})


def get_fact(fact_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Fact]:
    c = conn or get_connection()
    row = c.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    return _fact_from_row(row) if row else None


def list_facts(
    subject_type: Optional[str] = None,
    subject_id: Optional[str] = None,
    include_dismissed: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Fact]:
    c = conn or get_connection()
    clauses, params = [], []
    if not include_dismissed:
        clauses.append("status = 'active'")
    if subject_type:
        clauses.append("subject_type = ?")
        params.append(subject_type)
    if subject_id:
        clauses.append("subject_id = ?")
        params.append(subject_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = c.execute(f"SELECT * FROM facts {where} ORDER BY updated_at DESC", params).fetchall()
    return [_fact_from_row(r) for r in rows]


# --------------------------------------------------- conversation memory
def add_turn(
    session_id: str,
    role: str,
    text: str,
    facts: Optional[List[Dict[str, Any]]] = None,
    trace: Optional[List[Dict[str, Any]]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Append one turn to a conversation. Returns the turn id."""
    c = conn or get_connection()
    turn_id = new_id()
    c.execute(
        "INSERT INTO conversation_turns (id, session_id, role, text, facts, trace, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (turn_id, session_id, role, text, json.dumps(facts or []), json.dumps(trace or []), now_iso()),
    )
    c.commit()
    return turn_id


def conversation_turns(
    session_id: str, limit: int = 40, conn: Optional[sqlite3.Connection] = None
) -> List[Dict[str, Any]]:
    """A session's turns, oldest first — the transcript the client renders and
    the history the loop is primed with."""
    c = conn or get_connection()
    # created_at has second resolution and a turn pair is written inside one
    # second — rowid breaks the tie so the transcript can never invert.
    rows = c.execute(
        "SELECT * FROM (SELECT rowid AS rid, * FROM conversation_turns WHERE session_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?) ORDER BY created_at ASC, rid ASC",
        (session_id, limit),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "role": r["role"],
            "text": r["text"],
            "facts": json.loads(r["facts"]),
            "trace": json.loads(r["trace"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ----------------------------------------------------------------- misc
def purge_all(conn: Optional[sqlite3.Connection] = None) -> None:
    """§12: user can purge all extracted data and re-sync from scratch."""
    c = conn or get_connection()
    for table in (
        "conversation_turns",
        "watchers",
        "thread_closures",
        "findings",
        "thread_evidence",
        "threads",
        "facts",
        "loop_runs",
        "notifications",
        "completion_signals",
        "behavior_events",
        "items",
        "calendar_events",
        "messages",
        "conversations",
        "people",
        "model_weights",
        "sync_state",
    ):
        c.execute(f"DELETE FROM {table}")
    c.commit()
