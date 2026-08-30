-- Lifeline schema. Section 5 item schema plus the tables the ranking (§6),
-- completion (§7) and notification (§8.4) engines need to do their work.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- people
CREATE TABLE IF NOT EXISTS people (
    id              TEXT PRIMARY KEY,          -- stable slug, e.g. "sam-r"
    display_name    TEXT NOT NULL,
    relationship    TEXT,                      -- spouse|friend|family|other, from Contacts
    handles         TEXT NOT NULL DEFAULT '[]',-- JSON array of phone/email/whatsapp ids
    created_at      TEXT NOT NULL
);

-- --------------------------------------------------------------- threads
CREATE TABLE IF NOT EXISTS threads (
    id              TEXT PRIMARY KEY,          -- "<source>:<external id>"
    source          TEXT NOT NULL,             -- imessage|whatsapp|gmail
    display_name    TEXT NOT NULL,
    is_group        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

-- -------------------------------------------------------------- messages
-- Raw ingested text. Kept so extraction can run incrementally (§5) and so
-- the ranking engine can measure response latency (§6.2) against real replies.
CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    thread_id       TEXT NOT NULL REFERENCES threads(id),
    external_id     TEXT NOT NULL,             -- source-native id, for dedupe
    person_id       TEXT REFERENCES people(id),
    is_from_user    INTEGER NOT NULL DEFAULT 0,
    timestamp       TEXT NOT NULL,             -- ISO-8601
    text            TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}',-- JSON: subject, labels, starred...
    extracted_at    TEXT,                      -- NULL => not yet through extraction
    ingested_at     TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_pending  ON messages(extracted_at) WHERE extracted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_messages_thread   ON messages(thread_id, timestamp);

-- ------------------------------------------------------- calendar events
CREATE TABLE IF NOT EXISTS calendar_events (
    id              TEXT PRIMARY KEY,          -- google event id
    calendar_id     TEXT NOT NULL,
    summary         TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    location        TEXT NOT NULL DEFAULT '',
    start_at        TEXT,
    end_at          TEXT,
    status          TEXT NOT NULL DEFAULT 'confirmed',
    attendees       TEXT NOT NULL DEFAULT '[]',
    self_response   TEXT,                      -- RSVP: accepted|declined|needsAction
    updated_at      TEXT NOT NULL,
    ingested_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calendar_start ON calendar_events(start_at);

-- ----------------------------------------------------------------- items
-- The Section 5 output schema, one row per extracted item.
CREATE TABLE IF NOT EXISTS items (
    id                  TEXT PRIMARY KEY,
    source              TEXT NOT NULL,         -- imessage|whatsapp|gmail
    thread_id           TEXT NOT NULL,
    message_id          TEXT REFERENCES messages(id),
    person_id           TEXT REFERENCES people(id),
    person              TEXT NOT NULL,         -- denormalised display name
    timestamp           TEXT NOT NULL,
    type                TEXT NOT NULL,         -- purchase|event|promise|followup|reading|question
    raw_text            TEXT NOT NULL,
    entity_item         TEXT,
    entity_date         TEXT,                  -- ISO-8601, relative dates normalised
    entity_link         TEXT,
    suggested_action    TEXT NOT NULL DEFAULT '',
    suggested_reply     TEXT,
    status              TEXT NOT NULL DEFAULT 'pending', -- pending|completed|snoozed|dismissed
    -- ranking (§6), recomputed by the scorer
    score               REAL NOT NULL DEFAULT 0.0,
    interruption_level  TEXT NOT NULL DEFAULT 'active',  -- time_sensitive|active|passive
    score_explanation   TEXT NOT NULL DEFAULT '[]',      -- JSON, powers §8.3 "why here"
    behavior_pattern    TEXT,                  -- avoidance|deprioritized|NULL (§6.3)
    -- lifecycle
    snoozed_until       TEXT,
    completed_at        TEXT,
    completed_by        TEXT,                  -- auto|manual
    links_to_item_id    TEXT REFERENCES items(id),       -- followup back-reference
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, score DESC);
CREATE INDEX IF NOT EXISTS idx_items_thread ON items(thread_id);
CREATE INDEX IF NOT EXISTS idx_items_person ON items(person_id);

-- ------------------------------------------------------- behavior events
-- Every observable user behaviour. This is the training data for §6.
CREATE TABLE IF NOT EXISTS behavior_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT REFERENCES items(id),
    person_id   TEXT,
    item_type   TEXT,
    kind        TEXT NOT NULL,     -- surfaced|viewed|expanded|acted|snoozed|dismissed
                                   -- |completed_manual|completed_auto|replied
    occurred_at TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_behavior_item ON behavior_events(item_id, kind);
CREATE INDEX IF NOT EXISTS idx_behavior_pair ON behavior_events(person_id, item_type, kind);

-- ---------------------------------------------------- completion signals
CREATE TABLE IF NOT EXISTS completion_signals (
    id              TEXT PRIMARY KEY,
    item_id         TEXT NOT NULL REFERENCES items(id),
    source          TEXT NOT NULL,     -- gmail|calendar|manual
    evidence_ref    TEXT NOT NULL,     -- message/event id
    evidence_text   TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL,
    reasons         TEXT NOT NULL DEFAULT '[]',
    resolution      TEXT NOT NULL,     -- auto_closed|needs_confirmation|rejected|confirmed
    detected_at     TEXT NOT NULL,
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_item ON completion_signals(item_id, resolution);

-- --------------------------------------------------------- learned model
-- Weights start at the Section 6.2 static defaults (milestone 4) and are
-- updated by the learning loop (milestone 7).
CREATE TABLE IF NOT EXISTS model_weights (
    key         TEXT PRIMARY KEY,      -- "signal:sender_weight" | "person:sam-r"
                                       -- | "type:purchase" | "pair:sam-r/purchase"
    value       REAL NOT NULL,
    observations INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

-- ------------------------------------------------------------ sync state
CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,      -- gmail:history_id, calendar:sync_token, ...
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- ----------------------------------------------------- oauth credentials
CREATE TABLE IF NOT EXISTS oauth_tokens (
    provider        TEXT PRIMARY KEY,  -- google
    access_token    TEXT,
    refresh_token   TEXT,
    token_expiry    TEXT,
    scopes          TEXT NOT NULL DEFAULT '[]',
    updated_at      TEXT NOT NULL
);

-- --------------------------------------------------------- notifications
CREATE TABLE IF NOT EXISTS notifications (
    id              TEXT PRIMARY KEY,
    item_id         TEXT REFERENCES items(id),
    kind            TEXT NOT NULL,     -- item|passive_digest|completion
    interruption    TEXT NOT NULL,     -- time_sensitive|active|passive
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    sent_at         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_unsent ON notifications(sent_at) WHERE sent_at IS NULL;

CREATE TABLE IF NOT EXISTS devices (
    token           TEXT PRIMARY KEY,  -- APNs device token
    platform        TEXT NOT NULL DEFAULT 'ios',
    registered_at   TEXT NOT NULL
);

-- ------------------------------------------------------------- loop_runs
-- Provenance log of every agentic-loop investigation (§v1.4). Append-only;
-- powers the dossier "why", debugging, and don't-re-litigate.
CREATE TABLE IF NOT EXISTS loop_runs (
    id              TEXT PRIMARY KEY,
    trigger         TEXT NOT NULL,     -- ask|tell|ingest|sweep|curiosity
    goal            TEXT NOT NULL,
    provider        TEXT,              -- claude|gemini
    status          TEXT NOT NULL,     -- concluded|inconclusive
    iterations      INTEGER NOT NULL DEFAULT 0,
    tool_calls      TEXT NOT NULL DEFAULT '[]',  -- JSON [{name, input, result}]
    conclusion      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loop_runs_trigger ON loop_runs(trigger, created_at DESC);

-- ----------------------------------------------------------------- facts
-- The model of you (§v1.4 pillar B): first-person knowledge the user states
-- plus what the loop derives. Inspectable, editable, soft-deleted — the user
-- overrules the system, never the reverse.
CREATE TABLE IF NOT EXISTS facts (
    id              TEXT PRIMARY KEY,
    subject_type    TEXT NOT NULL,     -- self|person|topic
    subject_id      TEXT,              -- person_id / topic slug; NULL for self
    statement       TEXT NOT NULL,     -- human-readable ("wants the Meridian job")
    predicate       TEXT,              -- optional structured pair, e.g. priority
    value           TEXT,              -- ... = low
    source          TEXT NOT NULL,     -- user|derived
    confidence      REAL NOT NULL DEFAULT 1.0,
    provenance      TEXT,              -- loop_run id, or 'user'
    status          TEXT NOT NULL DEFAULT 'active',  -- active|dismissed
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject_type, subject_id, status);
