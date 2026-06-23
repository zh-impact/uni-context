package service

import (
	"context"
	"fmt"
	"os"

	"uni-context/internal/port"
)

// ReindexFTSService bulk-rewrites context_fts rows for items whose content
// was externalized (> ContentInlineLimit). The AFTER INSERT trigger on
// context_item reads new.content when writing the FTS row; for externalized
// items new.content is "" so the FTS row was indexed empty, making the item
// invisible to `search`. This service walks externalized items, hydrates
// their content from FileStore, and calls repo.ReindexFTS to rewrite the
// row with the real bytes.
//
// Constructed unconditionally (independent of embedder.enabled) because FTS
// search works in Plan 1 too — the bug is not embedding-specific.
//
// Idempotent: ReindexFTS uses a delete-then-insert pattern that yields one
// FTS row per call regardless of how many times it runs.
type ReindexFTSService struct {
	repo port.ContextRepo
	fs   port.FileStore
}

func NewReindexFTSService(repo port.ContextRepo, fs port.FileStore) *ReindexFTSService {
	return &ReindexFTSService{repo: repo, fs: fs}
}

// ReindexFailure records a single per-item error during a run.
type ReindexFailure struct {
	ItemID string
	Error  string
}

// ReindexReport summarizes one Run invocation. Scanned counts the
// externalized candidates found; Reindexed counts successful rewrites;
// Failed counts per-item failures (FileStore miss, FTS rewrite error).
type ReindexReport struct {
	Scanned   int
	Reindexed int
	Failed    int
	Failures  []ReindexFailure
}

// Run iterates items in the repo and rewrites FTS rows for any whose
// content was externalized (item.ContentURI != "" AND item.Content == "").
// Inline items are skipped — the AFTER INSERT trigger already indexed them
// correctly.
//
// For each externalized item:
//   - dryRun=true: increment Scanned only (no fs.Get, no ReindexFTS).
//   - dryRun=false: hydrate content from FileStore, call ReindexFTS,
//     record success or append to Failures.
//
// limit<=0 means no limit. List pages with the default cursor; this is a
// one-shot maintenance command so simplicity wins over throughput.
//
// Run does NOT return an error on per-item failures; the only error it
// returns is from the List call or ctx cancellation.
func (s *ReindexFTSService) Run(ctx context.Context, limit int, dryRun bool) (ReindexReport, error) {
	var report ReindexReport

	pageSize := 200
	if limit > 0 && limit < pageSize {
		pageSize = limit
	}
	var cursor string
	for {
		select {
		case <-ctx.Done():
			return report, ctx.Err()
		default:
		}
		if limit > 0 && report.Scanned >= limit {
			break
		}

		pageLimit := pageSize
		if limit > 0 {
			remaining := limit - report.Scanned
			if remaining < pageLimit {
				pageLimit = remaining
			}
		}

		items, next, err := s.repo.List(ctx, port.ItemFilter{
			Limit:  pageLimit,
			Cursor: cursor,
		})
		if err != nil {
			return report, fmt.Errorf("list items: %w", err)
		}
		if len(items) == 0 {
			break
		}

		for _, item := range items {
			if item.ContentURI == "" || item.Content != "" {
				// Inline items are already correctly indexed by the trigger.
				continue
			}
			if limit > 0 && report.Scanned >= limit {
				// Cap the candidate count even when List returns more
				// items than the limit (fakeRepo ignores Limit; real
				// sqlite honors it but a page may still overshoot if
				// items were added between pages).
				return report, nil
			}
			report.Scanned++

			if dryRun {
				continue
			}

			data, err := s.fs.Get(item.ContentURI)
			if err != nil {
				report.Failed++
				report.Failures = append(report.Failures, ReindexFailure{
					ItemID: item.ID,
					Error:  fmt.Sprintf("hydrate: %v", err),
				})
				continue
			}

			if err := s.repo.ReindexFTS(ctx, item.ID, item.Title, item.Summary, string(data)); err != nil {
				report.Failed++
				report.Failures = append(report.Failures, ReindexFailure{
					ItemID: item.ID,
					Error:  err.Error(),
				})
				continue
			}
			report.Reindexed++
		}

		if next == "" {
			break
		}
		cursor = next

		fmt.Fprintf(os.Stderr, "reindex-fts: %d items scanned\n", report.Scanned)
	}
	return report, nil
}
