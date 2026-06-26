-- Plan 2b: retry tracking for embeddings.
-- Additive ALTER only — does not rewrite 0002. The original `error` column
-- from 0002 is kept for backward-compat; `last_error` is the most recent
-- error text (worker updates it on every failed retry).

ALTER TABLE context_embedding ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE context_embedding ADD COLUMN last_error TEXT;

UPDATE schema_meta SET value = '3' WHERE key = 'schema_version';
