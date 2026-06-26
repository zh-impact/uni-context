-- Schema metadata
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '0');

-- Project (basic, for forward-compat with later plans)
CREATE TABLE IF NOT EXISTS project (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    path        TEXT,
    description TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

-- Core item table (full schema per spec §3.1)
CREATE TABLE IF NOT EXISTS context_item (
    id              TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    source          TEXT NOT NULL,
    owner_user_id   TEXT,
    project_id      TEXT REFERENCES project(id) ON DELETE SET NULL,
    agent_id        TEXT,
    conversation_id TEXT,
    parent_id       TEXT,
    title           TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    content         TEXT NOT NULL DEFAULT '',
    content_uri     TEXT,
    content_mime    TEXT,
    content_hash    TEXT,
    language        TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',
    source_meta     TEXT NOT NULL DEFAULT '{}',
    visibility      TEXT NOT NULL DEFAULT 'private',
    confidence      REAL NOT NULL DEFAULT 1.0,
    word_count      INTEGER NOT NULL DEFAULT 0,
    any_embedding   INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_item_scope_created ON context_item(scope, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_item_project       ON context_item(project_id) WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_item_kind          ON context_item(kind);
CREATE INDEX IF NOT EXISTS idx_item_owner         ON context_item(owner_user_id) WHERE owner_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_item_hash          ON context_item(content_hash) WHERE content_hash IS NOT NULL;

-- FTS5 (trigram tokenizer for CJK friendliness)
CREATE VIRTUAL TABLE IF NOT EXISTS context_fts USING fts5(
    title, summary, content,
    content='context_item', content_rowid='rowid',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS context_ai AFTER INSERT ON context_item BEGIN
    INSERT INTO context_fts(rowid, title, summary, content)
    VALUES (new.rowid, new.title, new.summary, new.content);
END;

CREATE TRIGGER IF NOT EXISTS context_ad AFTER DELETE ON context_item BEGIN
    INSERT INTO context_fts(context_fts, rowid, title, summary, content)
    VALUES ('delete', old.rowid, old.title, old.summary, old.content);
END;

CREATE TRIGGER IF NOT EXISTS context_au AFTER UPDATE ON context_item BEGIN
    INSERT INTO context_fts(context_fts, rowid, title, summary, content)
    VALUES ('delete', old.rowid, old.title, old.summary, old.content);
    INSERT INTO context_fts(rowid, title, summary, content)
    VALUES (new.rowid, new.title, new.summary, new.content);
END;

UPDATE schema_meta SET value = '1' WHERE key = 'schema_version';
