package service

import (
	"context"
	"fmt"
	"io"

	"uni-context/internal/port"
)

// BackfillService bulk-embeds items where any_embedding=0. Idempotent:
// items already embedded (any_embedding=1) are excluded by the
// ItemFilter.AnyEmbedding pre-filter, so they never enter iteration.
// Failures during the run are recorded but do not abort — Run returns
// a BackfillReport summarizing what happened.
type BackfillService struct {
	repo  port.ContextRepo
	embed *EmbedService
	// log receives per-100-item progress lines. Injected via constructor
	// so tests can assert on progress and the service has no direct
	// os.Stderr coupling.
	log io.Writer
}

// NewBackfillService wires the ContextRepo (for listing unembedded items),
// EmbedService (for embedding each one), and a logger for progress.
func NewBackfillService(repo port.ContextRepo, embed *EmbedService, log io.Writer) *BackfillService {
	return &BackfillService{repo: repo, embed: embed, log: log}
}

// BackfillFailure records a single per-item embed error during a run.
// Aggregated in BackfillReport.Failures so the CLI can surface them.
type BackfillFailure struct {
	ItemID string
	Error  string
}

// BackfillReport summarizes one Run invocation. Scanned counts the
// candidates found (any_embedding=0); Embedded counts successful embeds
// this run; Failed counts per-item failures. There is no Skipped field:
// the AnyEmbedding pre-filter excludes already-embedded items before
// iteration begins, so there is nothing to skip.
type BackfillReport struct {
	Scanned  int
	Embedded int
	Failed   int
	Failures []BackfillFailure
}

// Run iterates items where any_embedding=0 and embeds each one. For each
// item:
//   - dryRun=true: increment Scanned only (no embed, no status row).
//   - dryRun=false: call EmbedService.Embed; on failure record a
//     BackfillFailure and continue; on success increment Embedded.
//
// limit<=0 means no limit. The ContextRepo.List path treats limit<=0 or
// >200 as the default page size (50), which is acceptable for backfill —
// operators with very large corpora should re-run or raise the page cap.
// Progress is logged to stderr every 100 items.
//
// Run itself does NOT return an error on per-item embed failures; the
// only error it returns is from the initial List call or ctx cancellation.
func (s *BackfillService) Run(ctx context.Context, limit int, dryRun bool) (BackfillReport, error) {
	var report BackfillReport

	// Filter to unembedded items only. AnyEmbedding is *int; we take the
	// address of a zero value so the sqlite List honors it as "any_embedding=0".
	// (nil pointer would mean "no filter" and include already-embedded items.)
	anyEmbedZero := 0
	items, _, err := s.repo.List(ctx, port.ItemFilter{
		AnyEmbedding: &anyEmbedZero,
		Limit:        limit,
	})
	if err != nil {
		return report, fmt.Errorf("list unembedded items: %w", err)
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
			report.Failures = append(report.Failures, BackfillFailure{
				ItemID: item.ID,
				Error:  err.Error(),
			})
			continue
		}
		report.Embedded++

		if (i+1)%100 == 0 {
			fmt.Fprintf(s.log, "backfill: %d items processed\n", i+1)
		}
	}
	return report, nil
}
