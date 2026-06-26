-- Plan 2c follow-up: harden context_embedding.model_slug FK to ON DELETE CASCADE.
-- SQLite does not support ALTER TABLE ADD FOREIGN KEY; standard rebuild dance.
--
-- Note: the migrations runner (migrations.go execMigration) wraps each
-- file's body in a single tx via BeginTx/Commit, so this file MUST NOT
-- contain its own BEGIN/COMMIT (SQLite rejects nested BEGIN). PRAGMA
-- foreign_keys is a no-op inside a tx (SQLite docs), so it is omitted.
-- The rebuild is safe without disabling FKs because no other table
-- REFERENCES context_embedding -- it only holds FKs TO context_item and
-- embedding_model, which remain stable across the rebuild. The DROP of
-- the old context_embedding doesn't violate any FK (nothing references
-- it); the RENAME installs the new table in place.

CREATE TABLE context_embedding_new (
    item_id     TEXT NOT NULL REFERENCES context_item(id) ON DELETE CASCADE,
    model_slug  TEXT NOT NULL REFERENCES embedding_model(slug) ON DELETE CASCADE,
    embedded_at INTEGER NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    PRIMARY KEY (item_id, model_slug)
);

INSERT INTO context_embedding_new (item_id, model_slug, embedded_at, status, error, attempts, last_error)
SELECT item_id, model_slug, embedded_at, status, error, attempts, last_error
FROM context_embedding;

DROP TABLE context_embedding;
ALTER TABLE context_embedding_new RENAME TO context_embedding;

CREATE INDEX IF NOT EXISTS idx_emb_model ON context_embedding(model_slug);

UPDATE schema_meta SET value = '4' WHERE key = 'schema_version';
