package port

import "context"

// ModelDescriptor is the full projection of an embedding_model row.
// BaseURL and APIKey come from the row's config JSON column
// ({"base_url":"...","api_key":"..."}).
type ModelDescriptor struct {
	Slug      string
	Name      string
	Provider  string
	BaseURL   string
	APIKey    string
	Dimension int
	VecTable  string
	IsDefault bool
	Status    string // "active" | "disabled"
}

// ModelSpec is the input to ModelRegistry.Register.
type ModelSpec struct {
	Slug      string
	Provider  string
	BaseURL   string
	APIKey    string
	Dimension int
}

// ModelRegistry owns the embedding_model table. Methods are concurrency-safe
// via the underlying *sql.DB's connection pool; callers do not need external
// locking.
type ModelRegistry interface {
	// List returns all registered models ordered by created_at ASC.
	List(ctx context.Context) ([]ModelDescriptor, error)

	// GetActive returns the row with is_default=1. Returns a wrapping of
	// domain.ErrNotFound if no row is default.
	GetActive(ctx context.Context) (ModelDescriptor, error)

	// Get returns the row for slug. Returns a wrapping of domain.ErrNotFound
	// if slug is not registered.
	Get(ctx context.Context, slug string) (ModelDescriptor, error)

	// Register inserts a new model row and creates its vec_<slug>_<dim>
	// virtual table. Strict INSERT: returns an error if slug already exists.
	// Callers needing upsert behavior (e.g. reconcilePlan2cSync) must
	// explicitly check Get first and call UpdateConfig.
	Register(ctx context.Context, spec ModelSpec) error

	// UpdateConfig overwrites provider + config JSON for an existing slug.
	// Used by the first-Plan-2c-run reconciliation to heal Plan 2b alias
	// rows whose config column was '{}'. Returns a wrapping of
	// domain.ErrNotFound if slug does not exist.
	UpdateConfig(ctx context.Context, slug, baseURL, apiKey, provider string) error

	// SetDefault flips is_default atomically: slug gets 1, all others get 0.
	// Idempotent if slug is already default. Returns a wrapping of
	// domain.ErrNotFound if slug does not exist.
	SetDefault(ctx context.Context, slug string) error

	// Remove drops the model's vec table and deletes its embedding_model row.
	// context_embedding rows cascade-delete via FK ON DELETE CASCADE.
	// Refuses with a clear error if:
	//   - slug does not exist (wraps domain.ErrNotFound)
	//   - slug is_default=1 (caller must switch first)
	//   - vec_table is referenced by another slug (shared-table protection)
	Remove(ctx context.Context, slug string) error
}
