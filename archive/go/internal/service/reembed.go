package service

import (
	"context"
	"fmt"
	"io"

	"uni-context/internal/port"
)

// ReembedService bulk-embeds items under the active model. The filter
// differs from BackfillService: BackfillService targets items where
// any_embedding=0 (first-time embed), while ReembedService targets items
// that lack a status='done' row for the active model (migration to a new
// active model after `embed switch`).
//
// Idempotent: re-runs skip items already done for the active model.
// Resumable: failed items get status='failed' rows and are picked up by
// the worker (which is model-agnostic).
type ReembedService struct {
	repo   port.ContextRepo
	embed  *EmbedService
	active port.ModelInfo // slug of the currently-wired embedder
	// log receives per-100-item progress lines. Injected via constructor
	// so tests can assert on progress and the service has no direct
	// os.Stderr coupling.
	log io.Writer
}

// NewReembedService wires the ContextRepo (lists candidate items) +
// EmbedService (embeds each) + the active model identifier (filter key)
// + a logger for progress reporting.
func NewReembedService(repo port.ContextRepo, embed *EmbedService, active port.ModelInfo, log io.Writer) *ReembedService {
	return &ReembedService{repo: repo, embed: embed, active: active, log: log}
}

// ReembedFailure records a per-item embed error. Aggregated in
// ReembedService.Run's report so the CLI can surface them.
type ReembedFailure struct {
	ItemID string
	Error  string
}

// ReembedReport summarizes one Run invocation. Scanned = candidates
// found (no done row for active model); Embedded = successful embeds;
// Failed = per-item failures.
type ReembedReport struct {
	Scanned  int
	Embedded int
	Failed   int
	Failures []ReembedFailure
}

// Run iterates items lacking a status='done' row for the active model
// and embeds each. For each item:
//   - dryRun=true: increment Scanned only.
//   - dryRun=false: call EmbedService.Embed; on failure record a
//     ReembedFailure and continue; on success increment Embedded.
//
// limit<=0 means no limit. Progress is logged to stderr every 100 items.
//
// Run does NOT return an error on per-item embed failures; the only error
// it returns is from the initial List call or ctx cancellation.
func (s *ReembedService) Run(ctx context.Context, limit int, dryRun bool) (ReembedReport, error) {
	var report ReembedReport

	items, _, err := s.repo.List(ctx, port.ItemFilter{
		NotDoneForModel: s.active.Slug,
		Limit:           limit,
	})
	if err != nil {
		return report, fmt.Errorf("list items pending for model %s: %w", s.active.Slug, err)
	}

	for i, item := range items {
		select {
		case <-ctx.Done():
			return report, ctx.Err()
		default:
		}

		report.Scanned++
		if dryRun {
			continue
		}

		if err := s.embed.Embed(ctx, item.ID, item.Title, item.Content); err != nil {
			report.Failed++
			report.Failures = append(report.Failures, ReembedFailure{
				ItemID: item.ID,
				Error:  err.Error(),
			})
			continue
		}
		report.Embedded++

		if (i+1)%100 == 0 {
			fmt.Fprintf(s.log, "reembed: %d items processed\n", i+1)
		}
	}
	return report, nil
}
