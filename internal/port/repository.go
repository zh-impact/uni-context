package port

import (
	"context"

	"uni-context/internal/domain"
)

// ItemFilter narrows a list/search query.
type ItemFilter struct {
	Scopes      []domain.Scope
	Kinds       []domain.Kind
	Tags        []string // OR semantics: item matches if it has any of these tags
	OwnerUserID string
	ProjectID   string
	Cursor      string // opaque; created_at + id encoded
	Limit       int

	// AnyEmbedding filters by context_item.any_embedding. Pointer-style
	// so the zero value (nil) means "no filter" — existing callers from
	// Plan 1/2a are unchanged. When non-nil:
	//   *0 = only items NOT yet embedded
	//   *1 = only items already embedded
	AnyEmbedding *int

	// NotDoneForModel, when non-empty, restricts results to items that
	// lack a status='done' row in context_embedding for this model_slug.
	// Used by ReembedService to find items pending migration to a new
	// active model. Plan 2c addition.
	NotDoneForModel string
}

// ContextRepo is the persistence port for ContextItem.
type ContextRepo interface {
	Create(ctx context.Context, item domain.ContextItem) error
	Get(ctx context.Context, id string) (domain.ContextItem, error)
	Update(ctx context.Context, item domain.ContextItem) (domain.ContextItem, error)
	Delete(ctx context.Context, id string) error
	List(ctx context.Context, filter ItemFilter) ([]domain.ContextItem, string, error)
	// NextCursor builds an opaque cursor from the last item returned.
	NextCursor(item domain.ContextItem) string
}

// ProjectRepo is the persistence port for Project.
type ProjectRepo interface {
	Create(ctx context.Context, p domain.Project) error
	GetByName(ctx context.Context, name string) (domain.Project, error)
	List(ctx context.Context) ([]domain.Project, error)
	Delete(ctx context.Context, id string) error
}
