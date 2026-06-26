package service

import (
	"context"
	"fmt"
	"io"
	"strings"

	"uni-context/internal/port"
)

// EmbedService writes embeddings for items.
//
// Plan 2b changes vs Plan 2a:
//   - Hydrates content from FileStore when the caller passes empty content
//     and the item has a ContentURI (fixes the Plan 2a gap where items
//     with externalized content embedded title-only).
//   - Writes a context_embedding status row on every attempt (done/failed)
//     via EmbeddingRepo, so the worker can find retries and operators get
//     observability.
//
// Plan 2b does NOT add: async queue (sync ingest stays; worker cmd handles
// retries), max-attempts cap, multi-model parallel embed.
type EmbedService struct {
	embedder port.Embedder
	vs       port.VectorStore
	repo     port.ContextRepo
	fs       port.FileStore
	embRepo  port.EmbeddingRepo
	// log receives the non-fatal "could not record embedding status"
	// warning. Injected via constructor so tests can assert on warnings
	// and the service has no direct os.Stderr coupling.
	log io.Writer
}

// NewEmbedService wires the embedder, vector store, context repo,
// filestore (for content hydration), embedding status repo, and a logger
// for non-fatal status-write warnings.
func NewEmbedService(
	embedder port.Embedder,
	vs port.VectorStore,
	repo port.ContextRepo,
	fs port.FileStore,
	embRepo port.EmbeddingRepo,
	log io.Writer,
) *EmbedService {
	return &EmbedService{
		embedder: embedder, vs: vs, repo: repo, fs: fs, embRepo: embRepo, log: log,
	}
}

// Embed computes and stores an embedding for itemID.
//
// When content=="" and the item has ContentURI set (externalized case),
// Embed hydrates content from FileStore. Callers may pass content directly
// to skip the hydration round-trip (the backfill path may already have it).
//
// Side effects:
//   - vec_<model> row written (or replaced) for itemID
//   - context_item.any_embedding set to 1 via repo.Update (on success only)
//   - context_embedding status row written via embRepo.UpsertStatus
//
// Status row is written on EVERY attempt. On success: status='done',
// errStr="". On any failure: status='failed', errStr=err.Error().
// Status-row write failure is logged to stderr but does NOT mask the
// original embed error or success.
func (s *EmbedService) Embed(ctx context.Context, itemID, title, content string) error {
	model := s.embedder.Model().Slug

	// Hydrate if the caller didn't supply content. This is the path
	// IngestService.Create takes for externalized items (item.Content was
	// cleared after fs.Put). Backfill may pre-hydrate and pass content.
	hydratedContent := content
	if hydratedContent == "" {
		hc, err := s.hydrateContent(ctx, itemID)
		if err != nil {
			// Hydration failure is recoverable by the worker later; record status.
			s.recordStatus(ctx, itemID, model, "failed", err.Error())
			return fmt.Errorf("hydrate content for %s: %w", itemID, err)
		}
		hydratedContent = hc
	}

	text := strings.TrimSpace(title + "\n\n" + hydratedContent)
	if text == "" {
		err := fmt.Errorf("embed: empty text for item %s", itemID)
		s.recordStatus(ctx, itemID, model, "failed", err.Error())
		return err
	}

	vecs, err := s.embedder.Embed(ctx, []string{text})
	if err != nil {
		s.recordStatus(ctx, itemID, model, "failed", err.Error())
		return fmt.Errorf("embed item %s: %w", itemID, err)
	}
	if len(vecs) != 1 {
		err := fmt.Errorf("embedder returned %d vectors, expected 1", len(vecs))
		s.recordStatus(ctx, itemID, model, "failed", err.Error())
		return err
	}

	if err := s.vs.Put(ctx, model, itemID, vecs[0]); err != nil {
		s.recordStatus(ctx, itemID, model, "failed", err.Error())
		return fmt.Errorf("store vector for %s: %w", itemID, err)
	}

	// Flip any_embedding=1 so SearchService knows this item is vector-searchable.
	item, err := s.repo.Get(ctx, itemID)
	if err != nil {
		// Vec row already written; do NOT fail the whole embed over the flag.
		// Record status as done — the vec row IS the source of truth for
		// "embedded"; the any_embedding flag is a perf optimization, not
		// correctness. Surface the error so the caller knows the flag wasn't set.
		s.recordStatus(ctx, itemID, model, "done", "")
		return fmt.Errorf("load item for flag update: %w", err)
	}
	item.AnyEmbedding = 1
	if _, err := s.repo.Update(ctx, item); err != nil {
		s.recordStatus(ctx, itemID, model, "done", "")
		return fmt.Errorf("mark any_embedding: %w", err)
	}

	s.recordStatus(ctx, itemID, model, "done", "")
	return nil
}

// hydrateContent returns the item's inline Content if set, or fetches it
// from FileStore via ContentURI. Returns empty string if neither is set
// (which Embed treats as title-only — caller's responsibility to decide
// if that's acceptable).
func (s *EmbedService) hydrateContent(ctx context.Context, itemID string) (string, error) {
	item, err := s.repo.Get(ctx, itemID)
	if err != nil {
		return "", err
	}
	if item.Content != "" {
		return item.Content, nil
	}
	if item.ContentURI == "" {
		return "", nil // neither inline nor externalized; title-only embed
	}
	bytes, err := s.fs.Get(item.ContentURI)
	if err != nil {
		return "", fmt.Errorf("fs.Get %s: %w", item.ContentURI, err)
	}
	return string(bytes), nil
}

// recordStatus wraps embRepo.UpsertStatus with log logging on failure.
// Status-row write failure must never mask the original embed result.
func (s *EmbedService) recordStatus(ctx context.Context, itemID, model, status, errStr string) {
	if err := s.embRepo.UpsertStatus(ctx, itemID, model, status, errStr); err != nil {
		fmt.Fprintf(s.log,
			"warn: failed to record embedding status for %s: %v\n", itemID, err)
	}
}
