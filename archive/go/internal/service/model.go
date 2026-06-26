package service

import (
	"context"

	"uni-context/internal/port"
)

// ModelService is the application-layer boundary for embedding-model
// lifecycle and per-item embedding status. It is a thin pass-through over
// port.ModelRegistry + port.EmbeddingRepo — the value is the boundary,
// not added logic. Routing `embed model add/list/remove`, `embed switch`,
// and `embed status` through this service means the CLI has no direct
// dependency on those ports, so the registry implementation can change
// (e.g. switch to a different metadata store) without touching the
// inbound layer.
type ModelService struct {
	registry port.ModelRegistry
	embRepo  port.EmbeddingRepo
}

// NewModelService wires the model registry (lifecycle) + embedding status
// repo (per-item rows). Both are required; the "embedder not enabled"
// guard lives in the CLI — when Plan 1 is active the service is not
// constructed at all (App.Models stays nil).
func NewModelService(registry port.ModelRegistry, embRepo port.EmbeddingRepo) *ModelService {
	return &ModelService{registry: registry, embRepo: embRepo}
}

// AddModel registers a new embedding model and creates its vec table.
// Forwards verbatim to ModelRegistry.Register — slug conflicts, dimension
// validation, and provider checks are the registry's responsibility.
func (s *ModelService) AddModel(ctx context.Context, spec port.ModelSpec) error {
	return s.registry.Register(ctx, spec)
}

// ListModels returns every registered model ordered by created_at ASC.
// Forwards to ModelRegistry.List; the CLI's tabwriter formatting stays
// in the inbound layer.
func (s *ModelService) ListModels(ctx context.Context) ([]port.ModelDescriptor, error) {
	return s.registry.List(ctx)
}

// RemoveModel drops the model's vec table + deletes its embedding_model
// row. Refuses the default model and shared tables — those rules live in
// the registry and surface as errors here.
func (s *ModelService) RemoveModel(ctx context.Context, slug string) error {
	return s.registry.Remove(ctx, slug)
}

// SwitchModel flips is_default atomically to the named slug. Forwards to
// ModelRegistry.SetDefault. The post-switch "run reembed" reminder stays
// in the CLI — it's a UI concern, not a service-layer invariant.
func (s *ModelService) SwitchModel(ctx context.Context, slug string) error {
	return s.registry.SetDefault(ctx, slug)
}

// ItemEmbeddingStatus returns the context_embedding rows for one item
// across all models, ordered by model_slug ASC. Forwards to
// EmbeddingRepo.ListForItem — empty slice (not nil) when no rows exist.
func (s *ModelService) ItemEmbeddingStatus(ctx context.Context, itemID string) ([]port.EmbeddingStatus, error) {
	return s.embRepo.ListForItem(ctx, itemID)
}
