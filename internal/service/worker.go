package service

import (
	"context"
	"fmt"
	"os"
	"time"

	"uni-context/internal/port"
)

// WorkerService polls for status='failed' embeddings and retries them.
// Long-running: caller (CLI) cancels context on Ctrl+C.
//
// Plan 2b scope: fixed poll interval, no exponential backoff, no max-
// attempts cap. A row stays 'failed' until it succeeds; user can DELETE
// the row manually to skip an unrecoverable item (e.g. wrong model).
type WorkerService struct {
	repo    port.ContextRepo
	embRepo port.EmbeddingRepo
	embed   *EmbedService
}

// NewWorkerService wires the context repo (for hydration), embedding
// status repo (for ListFailed), and EmbedService (which writes the new
// status row on each retry).
func NewWorkerService(repo port.ContextRepo, embRepo port.EmbeddingRepo, embed *EmbedService) *WorkerService {
	return &WorkerService{repo: repo, embRepo: embRepo, embed: embed}
}

// workerBatchSize caps how many failed rows one iteration pulls. 100 is
// large enough to drain a typical backlog quickly but small enough to
// keep each iteration bounded so the loop stays responsive to cancellation.
const workerBatchSize = 100

// RunOneIteration processes one batch of failed embeddings. Returns the
// number of items attempted (success or failure). Exposed for testing
// so tests don't need to deal with the loop + interval machinery.
//
// EmbedService.Embed handles the status row update internally (writes
// 'done' on success, 'failed' + attempts++ on failure); RunOneIteration
// must NOT call embRepo.UpsertStatus directly.
func (s *WorkerService) RunOneIteration(ctx context.Context) (int, error) {
	failed, err := s.embRepo.ListFailed(ctx, workerBatchSize)
	if err != nil {
		return 0, fmt.Errorf("list failed: %w", err)
	}

	processed := 0
	for _, st := range failed {
		select {
		case <-ctx.Done():
			return processed, ctx.Err()
		default:
		}

		// Fetch the item to get its title + (inline) content. EmbedService
		// will hydrate from FileStore if content was externalized.
		item, err := s.repo.Get(ctx, st.ItemID)
		if err != nil {
			// Item was deleted between failure and retry. Log + skip;
			// the ON DELETE CASCADE on context_embedding.item_id should
			// have removed the row already, but defensive.
			fmt.Fprintf(os.Stderr, "worker: item %s vanished: %v\n", st.ItemID, err)
			continue
		}

		// EmbedService.Embed handles status row update internally (writes
		// 'done' on success, 'failed' + attempts++ on failure).
		if err := s.embed.Embed(ctx, item.ID, item.Title, item.Content); err != nil {
			fmt.Fprintf(os.Stderr, "worker: retry failed for %s (attempt %d): %v\n",
				item.ID, st.Attempts+1, err)
		}
		processed++
	}
	return processed, nil
}

// Run loops RunOneIteration with the given interval until ctx is cancelled.
// Logs to stderr each iteration: "worker: processed N items, sleeping <interval>".
//
// The pre-iteration select on ctx.Done() ensures a pre-cancelled context
// returns immediately without doing any work, so tests and fast Ctrl+C
// right after startup don't fire RunOneIteration once needlessly.
func (s *WorkerService) Run(ctx context.Context, interval time.Duration) error {
	if interval <= 0 {
		interval = 30 * time.Second
	}
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		processed, err := s.RunOneIteration(ctx)
		if err != nil && err != context.Canceled {
			return err
		}
		fmt.Fprintf(os.Stderr, "worker: processed %d items, sleeping %s\n",
			processed, interval)

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(interval):
		}
	}
}
