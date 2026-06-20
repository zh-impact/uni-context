package cli

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/spf13/cobra"
)

var (
	backfillLimit  int
	backfillDryRun bool
	workerInterval time.Duration
)

// embedCmd is the parent for embedding-related subcommands. It has no
// RunE of its own — invoking `unictx embed` without a subcommand prints
// the cobra help text.
var embedCmd = &cobra.Command{
	Use:   "embed",
	Short: "Manage embeddings (backfill, worker)",
}

// embedBackfillCmd runs BackfillService.Run once over the unembedded
// corpus and exits. Idempotent: re-running is safe (already-embedded
// items are excluded by the AnyEmbedding pre-filter).
var embedBackfillCmd = &cobra.Command{
	Use:   "backfill",
	Short: "Embed all items where any_embedding=0 (idempotent)",
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadApp()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.Backfill == nil {
			return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
		}

		ctx := signalContext()
		report, err := a.Backfill.Run(ctx, backfillLimit, backfillDryRun)
		if err != nil {
			return err
		}

		if backfillDryRun {
			fmt.Printf("dry run: would embed %d items\n", report.Scanned)
			return nil
		}
		fmt.Printf("backfill complete: embedded=%d failed=%d scanned=%d\n",
			report.Embedded, report.Failed, report.Scanned)
		if len(report.Failures) > 0 {
			fmt.Println("failures:")
			for _, f := range report.Failures {
				fmt.Printf("  %s: %s\n", f.ItemID, f.Error)
			}
		}
		return nil
	},
}

// embedWorkerCmd is the long-running retry loop for status='failed'
// embeddings. Task 5 creates the command so `unictx embed worker` exists
// in --help; Task 6 populates App.Worker (currently nil) and replaces
// this RunE stub with the real loop.
var embedWorkerCmd = &cobra.Command{
	Use:   "worker",
	Short: "Long-running retry loop for status=failed embeddings (Ctrl+C to stop)",
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadApp()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.Worker == nil {
			// Task 6 wires App.Worker. Until then this command is a
			// placeholder so `unictx embed --help` lists it.
			return fmt.Errorf("worker not yet wired (Task 6)")
		}

		// Unreachable until Task 6 (the nil-check above returns first).
		// Kept here to document the intended shape so Task 6 can drop in
		// the real call without restructuring the RunE.
		ctx := signalContext()
		fmt.Fprintf(os.Stderr, "worker: polling every %s, Ctrl+C to stop\n", workerInterval)
		_ = ctx
		return fmt.Errorf("worker not yet wired (Task 6)")
	},
}

// signalContext returns a context cancelled by SIGINT/SIGTERM. Shared by
// long-running commands (backfill on a large corpus, worker) so Ctrl+C
// drains gracefully instead of cutting off mid-embed.
func signalContext() context.Context {
	ctx, cancel := context.WithCancel(context.Background())
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		cancel()
	}()
	return ctx
}

func init() {
	embedBackfillCmd.Flags().IntVar(&backfillLimit, "limit", 0,
		"max items to embed (0 = no limit)")
	embedBackfillCmd.Flags().BoolVar(&backfillDryRun, "dry-run", false,
		"count candidates without embedding")
	embedWorkerCmd.Flags().DurationVar(&workerInterval, "interval", 30*time.Second,
		"poll interval for failed-embedding retries")

	embedCmd.AddCommand(embedBackfillCmd)
	embedCmd.AddCommand(embedWorkerCmd)
	rootCmd.AddCommand(embedCmd)
}
