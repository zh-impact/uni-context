package service

import (
	"context"
	"fmt"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

// ItemService is the query-side use case for context items. It owns the
// externalization-hydration policy (Content inline vs ContentURI → FileStore)
// so inbound adapters (CLI) read items through a service instead of reaching
// into Repo + FileStore ports directly. The hydration mirrors
// EmbedService.hydrateContent (embed.go:125); consolidating the two onto a
// shared helper is a worthwhile follow-up but out of scope here.
type ItemService struct {
	repo port.ContextRepo
	fs   port.FileStore
}

// NewItemService wires the ContextRepo (Get/List/Delete) + FileStore (used
// only by Get to hydrate externalized content).
func NewItemService(repo port.ContextRepo, fs port.FileStore) *ItemService {
	return &ItemService{repo: repo, fs: fs}
}

// Get returns a fully-hydrated item: the inline Content if present, else the
// externalized body loaded from FileStore via ContentURI. The returned item's
// Content field is always the readable text when the item has content — never
// the empty post-Create state for externalized items. A title-only item (no
// Content, no ContentURI) returns with Content == "".
//
// repo.Get errors (including domain.ErrNotFound) propagate unwrapped so
// callers can distinguish missing-item from hydration failures. A FileStore
// miss is wrapped with the URI so dangling pointers are diagnosable.
func (s *ItemService) Get(ctx context.Context, id string) (domain.ContextItem, error) {
	item, err := s.repo.Get(ctx, id)
	if err != nil {
		return domain.ContextItem{}, err
	}
	if item.Content != "" {
		return item, nil
	}
	if item.ContentURI == "" {
		return item, nil // title-only; nothing to hydrate
	}
	bytes, err := s.fs.Get(item.ContentURI)
	if err != nil {
		return domain.ContextItem{}, fmt.Errorf("hydrate content %s: %w", item.ContentURI, err)
	}
	item.Content = string(bytes)
	return item, nil
}

// List delegates to repo.List with the caller's filter (scope/kind/tags/
// owner). Pagination cursor passes through unchanged.
func (s *ItemService) List(ctx context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
	return s.repo.List(ctx, f)
}

// Delete delegates to repo.Delete.
func (s *ItemService) Delete(ctx context.Context, id string) error {
	return s.repo.Delete(ctx, id)
}
