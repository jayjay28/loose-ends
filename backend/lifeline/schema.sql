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

-- --------------------------------------------------------------- conversations
CREATE TABLE IF NOT EXISTS conversations (
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
    conversation_id       TEXT NOT NULL REFERENCES conversations(id),
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
CREATE INDEX IF NOT EXISTS idx_messages_conversation   ON messages(conversation_id, timestamp);

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
    conversation_id           TEXT NOT NULL,
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
    -- surfacing axis (§v1.4): actions you owe vs information worth knowing
    kind                TEXT NOT NULL DEFAULT 'action',  -- action|information
    category            TEXT,                  -- information only: discovery|context|external
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
CREATE INDEX IF NOT EXISTS idx_items_conversation ON items(conversation_id);
CREATE INDEX IF NOT EXISTS idx_items_person ON items(person_id);

-- --------------------------------------------------------------- threads
-- §v2 step 1. A thread is an open loop in the user's head — the unit of the
-- product. Distinct from `conversations`, which is a chat (renamed in v3 to
-- free the word).
CREATE TABLE IF NOT EXISTS threads (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    summary             TEXT NOT NULL DEFAULT '',
    origin              TEXT NOT NULL DEFAULT 'user',
                        -- user|promoted-from-item|system-proposed|silence|urgency
    state               TEXT NOT NULL DEFAULT 'live',
                        -- proposed|live|quiet|resolved|archived
    key                 TEXT,              -- dedupe key for system producers
    deadline            TEXT,              -- ISO-8601
    deadline_source     TEXT,              -- inferred|user
    deadline_reason     TEXT,              -- why, in words
    deadline_evidence   TEXT NOT NULL DEFAULT '[]',  -- JSON [{kind, ref_id}]
    importance          REAL NOT NULL DEFAULT 0.5,   -- learned, step 7
    opened_at           TEXT NOT NULL,
    resolved_at         TEXT,
    resolved_by         TEXT,              -- user|evidence
    last_seen_at        TEXT,              -- when the user last opened it
    last_worked_at      TEXT,              -- when the worker loop last ran it
    autonomy            TEXT NOT NULL DEFAULT 'prepared',  -- silent|prepared|ask
    contact_person_id   TEXT,                              -- who to write to, if anyone
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_state ON threads(state, importance DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_key ON threads(key) WHERE key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_threads_contact ON threads(contact_person_id);

-- Items become evidence (§v2): rows a thread can claim, cite, and close
-- against. A join table rather than a column on items because one item can
-- serve several threads — a hotel confirmation is evidence for both "Puerto
-- Rico trip" and "August spending".
CREATE TABLE IF NOT EXISTS thread_evidence (
    thread_id       TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL DEFAULT 'item',  -- item|message|calendar_event
    ref_id          TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'claimed', -- claimed|founding
    note            TEXT,
    linked_at       TEXT NOT NULL,
    PRIMARY KEY (thread_id, kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_evidence_ref ON thread_evidence(kind, ref_id);

-- ------------------------------------------------------------- findings
-- §v2 step 4. What the worker loop brings back, attached to the thread that
-- wanted it. Replaces `items.kind = 'information'` (v1.4 discoveries), which
-- had no thread to belong to.
--
-- `kind = 'nothing'` is deliberate and load-bearing: "I looked and found
-- nothing" is a real result, it is what the grey marks on the lane's activity
-- track represent, and a system that only records its hits is lying about its
-- work.
CREATE TABLE IF NOT EXISTS findings (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL DEFAULT 'finding',  -- finding|action|nothing
    headline        TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    importance      REAL NOT NULL DEFAULT 0.5,
    evidence        TEXT NOT NULL DEFAULT '[]',       -- JSON [{kind, ref_id}]
    -- §v2.1: a move is an `action` finding with these filled in.
    move_kind       TEXT,                             -- send|decide|gather|do
    steps           TEXT NOT NULL DEFAULT '[]',       -- JSON [] — the staged work
    needs           TEXT NOT NULL DEFAULT '[]',       -- JSON [] — what only the user can do
    blocked_reason  TEXT,                             -- named but not stageable, and why
    -- §v2.3: verified figures with sources, [{label, value, url}]. Legal on
    -- any kind — a finding that researched hard needs somewhere to put what it
    -- learned that isn't a paragraph.
    facts           TEXT NOT NULL DEFAULT '[]',
    loop_run_id     TEXT,                             -- provenance, always
    created_at      TEXT NOT NULL,
    surfaced_at     TEXT,                             -- when it reached the user
    dismissed_at    TEXT,
    -- §v2.3: a newer finding of the same kind replaced this one. Superseded,
    -- never deleted — the history is the receipt that the system was working.
    superseded_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_thread ON findings(thread_id, created_at DESC);

-- ------------------------------------------------------ thread closures
-- §v2 step 5. Mirrors `completion_signals` in shape so "closed on its own"
-- versus "asked me" feels the same as it always has, but keyed on threads,
-- which have none of an item's entity fields to match on.
CREATE TABLE IF NOT EXISTS thread_closures (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    confidence      REAL NOT NULL,
    reasons         TEXT NOT NULL DEFAULT '[]',
    evidence        TEXT NOT NULL DEFAULT '[]',
    evidence_key    TEXT NOT NULL DEFAULT '',   -- fingerprint; never ask twice
    resolution      TEXT NOT NULL,  -- auto_closed|needs_confirmation|confirmed|rejected
    detected_at     TEXT NOT NULL,
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_closures_thread ON thread_closures(thread_id, resolution);

-- ------------------------------------------------------------- watchers
-- §v2 step 6. "Is the flight delayed?" is not a question the user asked — it
-- is a standing monitor the thread implied. A watcher is deliberately dumb and
-- cheap: it runs on a cadence, checks local data with SQL, and when something
-- matches it *attaches evidence*. The worker loop is already evidence-
-- triggered, so it picks that up and does the interpreting. No LLM per check.
--
-- No web until step 8 — and mostly that isn't the limitation it sounds like.
-- Flight changes, bill amounts, delivery updates and school notices arrive in
-- the inbox already; watching for them is a parsing problem, not a web one.
CREATE TABLE IF NOT EXISTS watchers (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,     -- mail|messages|calendar|deadline
    spec            TEXT NOT NULL DEFAULT '{}',  -- JSON: what to look for
    what            TEXT NOT NULL DEFAULT '',    -- human-readable, for the UI
    cadence_minutes INTEGER NOT NULL DEFAULT 180,
    until           TEXT,              -- stop after this; NULL = open-ended
    state           TEXT NOT NULL DEFAULT 'active',  -- active|expired
    last_checked_at TEXT,
    last_fired_at   TEXT,
    fire_count      INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL DEFAULT 'worker',  -- worker|user
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watchers_thread ON watchers(thread_id, state);

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
    finding_id      TEXT REFERENCES findings(id),  -- §v2 7c: one push per finding
    kind            TEXT NOT NULL,     -- item|passive_digest|completion
    interruption    TEXT NOT NULL,     -- time_sensitive|active|passive
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    sent_at         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_unsent ON notifications(sent_at) WHERE sent_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_notifications_finding ON notifications(finding_id);

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
    trigger         TEXT NOT NULL,     -- ask|tell|converse|ingest|sweep|curiosity
    session_id      TEXT,              -- conversation this ran in, when any
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

-- ------------------------------------------------------- conversation_turns
-- The assistant's memory (§v1.5). Without this every /converse call is turn
-- zero: "her" has no referent and a bare follow-up ("Katie") can't attach to
-- what was just answered. Persisted, not client-held, so the conversation
-- survives relaunch like everything else the user is owed.
CREATE TABLE IF NOT EXISTS conversation_turns (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    role            TEXT NOT NULL,     -- user|assistant
    text            TEXT NOT NULL,
    facts           TEXT NOT NULL DEFAULT '[]',  -- JSON FactOut list (assistant turns)
    trace           TEXT NOT NULL DEFAULT '[]',  -- JSON TraceStepOut list
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON conversation_turns(session_id, created_at);

-- ------------------------------------------------------------- attachments
-- What a message carried besides its body (§v2.8 phase 0). One row per file
-- per message; `text` is what a parser got out of it, NULL until parsed.
-- A failure writes `error` and sets parsed_at so the row is done — one bad
-- PDF must never become a retry loop.
CREATE TABLE IF NOT EXISTS attachments (
    id              TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL REFERENCES messages(id),
    source          TEXT NOT NULL,     -- gmail|imessage
    remote_id       TEXT,              -- gmail attachmentId; chat.db attachment ROWID
    filename        TEXT,
    mime            TEXT,
    size_bytes      INTEGER,
    sha256          TEXT,              -- the same school form, mailed twice: parse once
    text            TEXT,              -- extracted text, capped; NULL until parsed
    parsed_at       TEXT,
    error           TEXT,              -- why parsing failed / was skipped
    ingested_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attachments_identity ON attachments(message_id, sha256);

-- ------------------------------------------------------------- full text
-- §v2.8 phase 1. External-content FTS5 over what people actually said and
-- what their mail carried. bm25-ranked, word-boundaried — the LIKE '%term%'
-- scan this replaces returned a term-life-insurance ad for a child's name.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content=messages, content_rowid=rowid
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts USING fts5(
    text, content=attachments, content_rowid=rowid
);
CREATE TRIGGER IF NOT EXISTS attachments_fts_ai AFTER INSERT ON attachments BEGIN
    INSERT INTO attachments_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS attachments_fts_ad AFTER DELETE ON attachments BEGIN
    INSERT INTO attachments_fts(attachments_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS attachments_fts_au AFTER UPDATE OF text ON attachments BEGIN
    INSERT INTO attachments_fts(attachments_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO attachments_fts(rowid, text) VALUES (new.rowid, new.text);
END;

-- ------------------------------------------------------------- the world
-- §v2.8 phase 2. Who and what the messages are about, accumulated — the
-- store's standing knowledge, as opposed to its events. People migrated in
-- keep their slug ids (seven tables point at them); everything new gets an
-- opaque id, which is what makes a future merge an alias rewrite instead of
-- a DELETE with dangling pointers.
CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,             -- person|place|org|arrangement
    name            TEXT NOT NULL,             -- canonical display name
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);

CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id       TEXT NOT NULL REFERENCES entities(id),
    alias           TEXT NOT NULL,             -- normalised: lowercased name, bare digits, bare email
    source          TEXT,                      -- contacts|signature|message|user|migration
    UNIQUE(alias, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_aliases_alias ON entity_aliases(alias);

CREATE TABLE IF NOT EXISTS entity_facts (
    id              TEXT PRIMARY KEY,
    entity_id       TEXT NOT NULL REFERENCES entities(id),
    predicate       TEXT NOT NULL,             -- attends|located_at|account_with|role_of|...
    value           TEXT NOT NULL,
    value_ref       TEXT,                      -- another entity id, when the value IS one
    confidence      REAL NOT NULL DEFAULT 0.8,
    message_id      TEXT REFERENCES messages(id),  -- the receipt
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active' -- active|superseded
);
CREATE INDEX IF NOT EXISTS idx_efacts_entity ON entity_facts(entity_id, status);

CREATE TABLE IF NOT EXISTS thread_entities (
    thread_id       TEXT NOT NULL REFERENCES threads(id),
    entity_id       TEXT NOT NULL REFERENCES entities(id),
    role            TEXT,                      -- subject|counterparty|institution|venue
    UNIQUE(thread_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_tent_entity ON thread_entities(entity_id);

-- -------------------------------------------------------------------- asks
-- §v2.9 — an ask is a card, not a chat turn. The question, the answer, the
-- receipts it rests on and the facts it drew from, kept so the Ask surface
-- is a reference that survives relaunch.
CREATE TABLE IF NOT EXISTS asks (
    id              TEXT PRIMARY KEY,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    receipts        TEXT NOT NULL DEFAULT '[]',   -- JSON ReceiptOut list
    knew            TEXT NOT NULL DEFAULT '[]',   -- JSON KnownFactOut list
    trace           TEXT NOT NULL DEFAULT '[]',   -- JSON TraceStepOut list
    loop_run_id     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asks_created ON asks(created_at);

-- ------------------------------------------------------------------ pairing
-- §v3 — the API stops being open. A device earns a bearer token by claiming
-- a short-lived pairing code minted on the Mac; the token is what every
-- request carries from then on. Secrets are stored hashed — the database
-- holds proof of the token, never the token.
CREATE TABLE IF NOT EXISTS api_tokens (
    id              TEXT PRIMARY KEY,
    secret_hash     TEXT NOT NULL,             -- sha256 of the bearer secret
    device_name     TEXT NOT NULL DEFAULT '',  -- "Alex's iPhone"
    created_at      TEXT NOT NULL,
    last_used_at    TEXT,
    revoked_at      TEXT
);

CREATE TABLE IF NOT EXISTS pairing_codes (
    code            TEXT PRIMARY KEY,          -- short, typed by a human
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,             -- ten minutes of validity
    claimed_at      TEXT,                      -- single use, spent on claim
    token_id        TEXT                       -- the token the claim minted
);
