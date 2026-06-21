package sqlite

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

type EmbeddingRepo struct {
	db *sql.DB
}

func NewEmbeddingRepo(db *sql.DB) *EmbeddingRepo {
	return &EmbeddingRepo{db: db}
}

// upsertSQL uses ON CONFLICT to atomically insert-or-update + increment.
// attempts+1 happens in the conflict branch; fresh INSERT starts at 1
// (the VALUES binds attempts=1 directly).
//
// last_error mirrors errStr (the most recent error). The original 0002
// `error` column is also set to errStr for backward-compat — it stays
// the same across retries, which is fine; last_error is the authoritative
// "what went wrong most recently" field.
const upsertSQL = `
INSERT INTO context_embedding
    (item_id, model_slug, embedded_at, status, error, last_error, attempts)
VALUES (?, ?, ?, ?, ?, ?, 1)
ON CONFLICT(item_id, model_slug) DO UPDATE SET
    embedded_at = excluded.embedded_at,
    status      = excluded.status,
    error       = excluded.error,
    last_error  = excluded.last_error,
    attempts    = context_embedding.attempts + 1
`

func (r *EmbeddingRepo) UpsertStatus(ctx context.Context, itemID, modelSlug, status, errStr string) error {
	now := time.Now().UTC().Unix()
	if _, err := r.db.ExecContext(ctx, upsertSQL,
		itemID, modelSlug, now, status, errStr, errStr); err != nil {
		return fmt.Errorf("upsert embedding status: %w", err)
	}
	return nil
}

const getStatusSQL = `
SELECT item_id, model_slug, status, error, last_error, attempts, embedded_at
FROM context_embedding
WHERE item_id = ? AND model_slug = ?
`

func (r *EmbeddingRepo) GetStatus(ctx context.Context, itemID, modelSlug string) (port.EmbeddingStatus, error) {
	var (
		s          port.EmbeddingStatus
		err1       sql.NullString
		lastErr    sql.NullString
		embeddedAt int64
	)
	err := r.db.QueryRowContext(ctx, getStatusSQL, itemID, modelSlug).Scan(
		&s.ItemID, &s.ModelSlug, &s.Status, &err1, &lastErr, &s.Attempts, &embeddedAt)
	if err == sql.ErrNoRows {
		return port.EmbeddingStatus{}, fmt.Errorf("%w: embedding %s/%s",
			domain.ErrNotFound, itemID, modelSlug)
	}
	if err != nil {
		return port.EmbeddingStatus{}, fmt.Errorf("get embedding status: %w", err)
	}
	s.Error = err1.String
	s.LastError = lastErr.String
	s.EmbeddedAt = time.Unix(embeddedAt, 0).UTC()
	return s, nil
}

const listFailedSQL = `
SELECT item_id, model_slug, status, error, last_error, attempts, embedded_at
FROM context_embedding
WHERE status = 'failed'
ORDER BY embedded_at ASC
LIMIT ?
`

func (r *EmbeddingRepo) ListFailed(ctx context.Context, limit int) ([]port.EmbeddingStatus, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := r.db.QueryContext(ctx, listFailedSQL, limit)
	if err != nil {
		return nil, fmt.Errorf("list failed embeddings: %w", err)
	}
	defer rows.Close()

	var out []port.EmbeddingStatus
	for rows.Next() {
		var (
			s          port.EmbeddingStatus
			err1       sql.NullString
			lastErr    sql.NullString
			embeddedAt int64
		)
		if err := rows.Scan(
			&s.ItemID, &s.ModelSlug, &s.Status, &err1, &lastErr, &s.Attempts, &embeddedAt); err != nil {
			return nil, err
		}
		s.Error = err1.String
		s.LastError = lastErr.String
		s.EmbeddedAt = time.Unix(embeddedAt, 0).UTC()
		out = append(out, s)
	}
	return out, rows.Err()
}

const listForItemSQL = `
SELECT item_id, model_slug, status, error, last_error, attempts, embedded_at
FROM context_embedding
WHERE item_id = ?
ORDER BY model_slug ASC
`

// ListForItem returns all status rows for the given item, ordered by
// model_slug ASC. Empty slice (not nil) if no rows — callers depend on
// `len(rows) == 0` without nil-checking. Used by `embed status <id>`.
func (r *EmbeddingRepo) ListForItem(ctx context.Context, itemID string) ([]port.EmbeddingStatus, error) {
	rows, err := r.db.QueryContext(ctx, listForItemSQL, itemID)
	if err != nil {
		return nil, fmt.Errorf("list status for item %s: %w", itemID, err)
	}
	defer rows.Close()

	out := []port.EmbeddingStatus{}
	for rows.Next() {
		var (
			s          port.EmbeddingStatus
			err1       sql.NullString
			lastErr    sql.NullString
			embeddedAt int64
		)
		if err := rows.Scan(
			&s.ItemID, &s.ModelSlug, &s.Status, &err1, &lastErr,
			&s.Attempts, &embeddedAt); err != nil {
			return nil, fmt.Errorf("scan status row: %w", err)
		}
		s.Error = err1.String
		s.LastError = lastErr.String
		s.EmbeddedAt = time.Unix(embeddedAt, 0).UTC()
		out = append(out, s)
	}
	return out, rows.Err()
}
