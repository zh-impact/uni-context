package port

import "context"

// VectorQuery is a KNN search against the vector store. Filters are
// applied via JOIN on context_item — see sqlite impl.
type VectorQuery struct {
	Vector []float32
	Model  string // slug, must match embedding_model.slug
	Limit  int
	// Filters pushed down to context_item (same semantics as ItemFilter
	// for the same fields). Empty = no filter.
	Scopes []string
	Kinds  []string
}

// VectorHit is one KNN result.
type VectorHit struct {
	ID       string
	Score    float64 // higher = better (caller-normalized from distance)
	Distance float64 // raw vec0 distance (lower = better)
}

// VectorStore reads and writes embeddings keyed by item_id. A given
// item_id has at most one embedding per model (PRIMARY KEY in
// context_embedding).
type VectorStore interface {
	// Put writes (or replaces) the embedding for item_id under the
	// given model. Idempotent.
	Put(ctx context.Context, model, itemID string, vector []float32) error
	// Search runs a KNN query. Returns hits sorted by Score DESC.
	Search(ctx context.Context, q VectorQuery) ([]VectorHit, error)
	// Delete removes the embedding for item_id under model. No-op if absent.
	Delete(ctx context.Context, model, itemID string) error
}
