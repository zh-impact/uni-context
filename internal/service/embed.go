package service

import (
	"context"
	"fmt"
	"strings"

	"uni-context/internal/port"
)

// EmbedService writes embeddings for items. Plan 2a: synchronous, single
// model (the embedder's Model().Slug). Plan 2b adds async queue + worker.
//
// Plan 2a does NOT write context_embedding status rows; the vec_<model>
// table presence IS the embedded signal. Plan 2b will add the status row
// (with status='done'/'failed' and error text) for retry tracking. The
// any_embedding=1 flag on context_item is the per-item "searchable by
// vector" indicator that SearchService checks.
type EmbedService struct {
	embedder port.Embedder
	vs       port.VectorStore
	repo     port.ContextRepo
}

func NewEmbedService(embedder port.Embedder, vs port.VectorStore, repo port.ContextRepo) *EmbedService {
	return &EmbedService{embedder: embedder, vs: vs, repo: repo}
}

// Embed computes and stores an embedding for itemID. The title and
// content are passed in (rather than re-fetched) so callers can compose
// the embed text however they like. Embed composes them as
// "title\n\ncontent". Errors from the embedder are returned; the caller
// decides whether to tolerate them (IngestService does) or fail.
//
// Side effects:
//   - vec_<model> row written (or replaced) for itemID
//   - context_item.any_embedding set to 1 via repo.Update
//
// Plan 2a limitation: no context_embedding status row is written (see
// the type doc). The vec_<model> row's presence is the embedded signal.
func (s *EmbedService) Embed(ctx context.Context, itemID, title, content string) error {
	model := s.embedder.Model().Slug
	text := strings.TrimSpace(title + "\n\n" + content)
	if text == "" {
		return fmt.Errorf("embed: empty text for item %s", itemID)
	}

	vecs, err := s.embedder.Embed(ctx, []string{text})
	if err != nil {
		return fmt.Errorf("embed item %s: %w", itemID, err)
	}
	if len(vecs) != 1 {
		return fmt.Errorf("embedder returned %d vectors, expected 1", len(vecs))
	}

	if err := s.vs.Put(ctx, model, itemID, vecs[0]); err != nil {
		return fmt.Errorf("store vector for %s: %w", itemID, err)
	}

	// Flip any_embedding=1 so SearchService knows this item is vector-searchable.
	item, err := s.repo.Get(ctx, itemID)
	if err != nil {
		return fmt.Errorf("load item for flag update: %w", err)
	}
	item.AnyEmbedding = 1
	if err := s.repo.Update(ctx, item); err != nil {
		return fmt.Errorf("mark any_embedding: %w", err)
	}
	return nil
}
