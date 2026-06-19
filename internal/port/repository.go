package port

import (
    "context"

    "uni-context/internal/domain"
)

// ItemFilter narrows a list/search query.
type ItemFilter struct {
    Scopes   []domain.Scope
    Kinds    []domain.Kind
    Tags     []string // AND semantics
    OwnerUserID string
    ProjectID   string
    Cursor   string // opaque; created_at + id encoded
    Limit    int
}

// ContextRepo is the persistence port for ContextItem.
type ContextRepo interface {
    Create(ctx context.Context, item domain.ContextItem) error
    Get(ctx context.Context, id string) (domain.ContextItem, error)
    Update(ctx context.Context, item domain.ContextItem) error
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
