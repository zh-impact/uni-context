package cli

import (
	"fmt"

	"github.com/spf13/cobra"
)

var (
	reindexFTSLimit  int
	reindexFTSDryRun bool
)

// reindexFTSCmd is the one-shot maintenance command for healing externalized
// items that pre-date the IngestService → ReindexFTS wiring. Walks all
// items, hydrates externalized content from FileStore, and rewrites the
// FTS row so `search` can find it.
//
// Idempotent: the underlying ReindexFTS uses a delete-then-insert pattern
// that produces one FTS row per item regardless of how many times it runs.
// Safe to re-run after interruptions.
//
// Inline items are skipped — their FTS rows were correctly populated by
// the AFTER INSERT trigger.
var reindexFTSCmd = &cobra.Command{
	Use:   "reindex-fts",
	Short: "Rewrite FTS rows for externalized items (heal pre-fix backfill)",
	Long: `Rewrite context_fts rows for items whose content was externalized
(> 4KB) before IngestService called ReindexFTS on Create.

The bug: the AFTER INSERT trigger on context_item reads new.content when
writing the FTS row. For externalized items new.content is "" (real bytes
live in FileStore), so the FTS row was indexed empty and 'search' could
not find the item even when the keyword appeared in the file.

This command iterates every item, hydrates externalized content from
FileStore, and calls ReindexFTS to rewrite the row with the real bytes.
Inline items are skipped (the trigger already handled them). Idempotent.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadApp()
		if err != nil {
			return err
		}
		defer a.Close()

		ctx := signalContext()
		report, err := a.ReindexFTS.Run(ctx, reindexFTSLimit, reindexFTSDryRun)
		if err != nil {
			return err
		}

		if reindexFTSDryRun {
			fmt.Printf("dry run: would reindex %d externalized items\n", report.Scanned)
			return nil
		}
		fmt.Printf("reindex complete: reindexed=%d failed=%d scanned=%d\n",
			report.Reindexed, report.Failed, report.Scanned)
		if len(report.Failures) > 0 {
			fmt.Println("failures:")
			for _, f := range report.Failures {
				fmt.Printf("  %s: %s\n", f.ItemID, f.Error)
			}
		}
		return nil
	},
}

func init() {
	reindexFTSCmd.Flags().IntVar(&reindexFTSLimit, "limit", 0,
		"max items to scan (0 = no limit)")
	reindexFTSCmd.Flags().BoolVar(&reindexFTSDryRun, "dry-run", false,
		"count externalized candidates without rewriting")
	rootCmd.AddCommand(reindexFTSCmd)
}
