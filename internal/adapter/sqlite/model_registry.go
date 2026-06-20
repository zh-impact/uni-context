package sqlite

import (
	"context"
	"database/sql"
	"fmt"
)

// EnsureModelRegistered guarantees the embedding_model table has a row
// for slug. Plan 2a ships with a single seed row ('bge-m3', 1024-dim),
// which is fine for Ollama users but breaks when a config uses a
// different slug — e.g. LMStudio exposes bge-m3 under the id
// `text-embedding-bge-m3`, and OpenAI users will pass `text-embedding-3-small`.
//
// Resolution strategy (Plan 2a constraint: only 1024-dim models supported
// without a new migration):
//   - If slug already registered → no-op.
//   - If slug is new but an existing embedding_model row has the same
//     dimension → reuse its vec_table (INSERT new slug pointing at it).
//     This lets any 1024-dim model share vec_bge_m3_1024 transparently.
//   - If slug is new AND no registered model has the requested dimension
//     → error. Adding a non-1024 model requires a new migration that
//     creates the corresponding vec_<slug>_<dim> table (Plan 2c work).
//
// Plan 2c will replace this with true multi-model registry: each model
// gets its own vec table created on first use.
func EnsureModelRegistered(db *sql.DB, slug, provider string, dimension int) error {
	// Fast path: slug already registered. The seed migration handles
	// 'bge-m3'; EnsureModelRegistered is a no-op for it.
	var existing string
	err := db.QueryRowContext(context.Background(),
		`SELECT slug FROM embedding_model WHERE slug = ?`, slug).Scan(&existing)
	if err == nil {
		return nil // already registered
	}
	if err != sql.ErrNoRows {
		return fmt.Errorf("check existing model %s: %w", slug, err)
	}

	// Find a vec_table registered for the same dimension. Plan 2a has
	// exactly one (vec_bge_m3_1024); if more are added later, the
	// first-seen wins, which is fine — they're structurally identical.
	var vecTable string
	err = db.QueryRowContext(context.Background(),
		`SELECT vec_table FROM embedding_model WHERE dimension = ? LIMIT 1`, dimension).Scan(&vecTable)
	if err == sql.ErrNoRows {
		return fmt.Errorf("model %s: dimension %d not registered; Plan 2a supports 1024 only (Plan 2c adds dynamic table creation)",
			slug, dimension)
	}
	if err != nil {
		return fmt.Errorf("lookup vec_table for dim %d: %w", dimension, err)
	}

	// Reuse the existing vec_table. is_default stays 0 — the seed row
	// remains the canonical default. config NULL → '{}' for parity with
	// the seed row.
	_, err = db.ExecContext(context.Background(),
		`INSERT OR IGNORE INTO embedding_model
		    (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
		 VALUES (?, ?, ?, ?, ?, 0, 'active', '{}', strftime('%s','now'))`,
		slug, slug, provider, dimension, vecTable)
	if err != nil {
		return fmt.Errorf("register model %s: %w", slug, err)
	}
	return nil
}
