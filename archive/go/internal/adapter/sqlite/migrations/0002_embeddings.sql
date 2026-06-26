-- Plan 2a: vector embeddings. Single default model (bge-m3, 1024-dim).
-- Multi-model registry is Plan 2c.

-- Model registry. is_default=1 constrained to at most one row at the
-- application layer (Plan 2a hardcodes one row, so this is trivially
-- true; Plan 2c adds a trigger or app-level check).
CREATE TABLE IF NOT EXISTS embedding_model (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    provider    TEXT NOT NULL,           -- ollama | openai-compat | onnx
    dimension   INTEGER NOT NULL,
    vec_table   TEXT NOT NULL,           --对应 vec0 表名
    is_default  INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
    status      TEXT NOT NULL DEFAULT 'active',
    config      TEXT NOT NULL DEFAULT '{}',
    created_at  INTEGER NOT NULL
);

-- Item × model N:N. status: done | failed. Primary key prevents dup
-- embeds for the same (item, model).
CREATE TABLE IF NOT EXISTS context_embedding (
    item_id     TEXT NOT NULL REFERENCES context_item(id) ON DELETE CASCADE,
    model_slug  TEXT NOT NULL REFERENCES embedding_model(slug),
    embedded_at INTEGER NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    PRIMARY KEY (item_id, model_slug)
);
CREATE INDEX IF NOT EXISTS idx_emb_model ON context_embedding(model_slug);

-- vec0 virtual table for the default model. cosine distance because
-- bge-m3 embeddings are typically consumed via cosine similarity.
CREATE VIRTUAL TABLE IF NOT EXISTS vec_bge_m3_1024 USING vec0(
    item_id TEXT PRIMARY KEY,
    embedding FLOAT[1024] distance_metric=cosine
);

-- Seed the default model row. Idempotent via INSERT OR IGNORE.
INSERT OR IGNORE INTO embedding_model
    (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
VALUES
    ('bge-m3', 'BGE M3', 'ollama', 1024, 'vec_bge_m3_1024', 1, 'active',
     '{"base_url":"http://localhost:11434","model":"bge-m3"}',
     strftime('%s','now'));
