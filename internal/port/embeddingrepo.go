package port

import (
	"context"
	"time"
)

// EmbeddingStatus is one row in context_embedding. Mirrors the schema
// from migrations 0002 + 0003.
type EmbeddingStatus struct {
	ItemID     string
	ModelSlug  string
	Status     string // "done" | "failed"
	Error      string // original error text (0002 column)
	LastError  string // most recent error text (0003 column)
	Attempts   int
	EmbeddedAt time.Time
}

// EmbeddingRepo owns the context_embedding table. Separate from
// ContextRepo because the table serves a different consumer (worker +
// observability) and mixing them produces a fat interface.
type EmbeddingRepo interface {
	// UpsertStatus inserts or updates the status row for (itemID, modelSlug).
	// On conflict, attempts is incremented by 1 (fresh INSERT starts at 1),
	// embedded_at is set to now, status/error/last_error are overwritten.
	UpsertStatus(ctx context.Context, itemID, modelSlug, status, errStr string) error

	// GetStatus returns the row for (itemID, modelSlug). Returns a
	// wrapping of domain.ErrNotFound if no row exists.
	GetStatus(ctx context.Context, itemID, modelSlug string) (EmbeddingStatus, error)

	// ListFailed returns up to limit rows with status='failed', ordered
	// by embedded_at ASC (oldest failures first — they've waited longest).
	// limit<=0 defaults to 100.
	ListFailed(ctx context.Context, limit int) ([]EmbeddingStatus, error)

	// ListForItem returns all status rows for the given item, ordered by
	// model_slug ASC. Empty slice (not nil) if no rows — callers depend
	// on `len(rows) == 0` without nil-checking. Used by the
	// `embed status <id>` CLI to show per-model migration state.
	// Plan 2c follow-up addition.
	ListForItem(ctx context.Context, itemID string) ([]EmbeddingStatus, error)
}
