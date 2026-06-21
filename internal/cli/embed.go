package cli

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"text/tabwriter"
	"time"

	"uni-context/internal/port"

	"github.com/spf13/cobra"
)

var (
	backfillLimit  int
	backfillDryRun bool
	workerInterval time.Duration
	reembedLimit   int
	reembedDryRun  bool
)

// loadAppFn is the indirection that enables RunE-level tests. Tests swap
// it to return a stubbed *App; production code leaves the default. Only
// embed.go uses this — other CLI files call loadApp() directly.
// Plan 2c follow-up addition.
var loadAppFn = loadApp

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
		a, _, err := loadAppFn()
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
// embeddings. Polls EmbeddingRepo.ListFailed at the configured interval,
// retries each via EmbedService.Embed (which writes the new status row),
// and exits cleanly on Ctrl+C (SIGINT/SIGTERM).
var embedWorkerCmd = &cobra.Command{
	Use:   "worker",
	Short: "Long-running retry loop for status=failed embeddings (Ctrl+C to stop)",
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadAppFn()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.Worker == nil {
			return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
		}

		ctx := signalContext()
		fmt.Fprintf(os.Stderr, "worker: polling every %s, Ctrl+C to stop\n", workerInterval)
		return a.Worker.Run(ctx, workerInterval)
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

// embedModelCmd is the parent for model-lifecycle subcommands. No RunE:
// invoking `unictx embed model` without a subcommand prints cobra help.
var embedModelCmd = &cobra.Command{
	Use:   "model",
	Short: "Manage embedding models (add/list/remove)",
}

// Flags for `embed model add`.
var (
	modelAddProvider string
	modelAddBaseURL  string
	modelAddDim      int
	modelAddAPIKey   string
)

var embedModelAddCmd = &cobra.Command{
	Use:   "add <slug>",
	Short: "Register a new embedding model (creates its vec table)",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadAppFn()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.Registry == nil {
			return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
		}

		slug := args[0]
		return a.Registry.Register(cmd.Context(), port.ModelSpec{
			Slug:      slug,
			Provider:  modelAddProvider,
			BaseURL:   modelAddBaseURL,
			APIKey:    modelAddAPIKey,
			Dimension: modelAddDim,
		})
	},
}

var embedModelListCmd = &cobra.Command{
	Use:   "list",
	Short: "List all registered embedding models",
	Args:  cobra.NoArgs,
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadAppFn()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.Registry == nil {
			return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
		}

		models, err := a.Registry.List(cmd.Context())
		if err != nil {
			return err
		}
		w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
		fmt.Fprintln(w, "SLUG\tPROVIDER\tDIM\tVEC_TABLE\tDEFAULT\tSTATUS")
		for _, m := range models {
			defaultMark := ""
			if m.IsDefault {
				defaultMark = "*"
			}
			fmt.Fprintf(w, "%s\t%s\t%d\t%s\t%s\t%s\n",
				m.Slug, m.Provider, m.Dimension, m.VecTable, defaultMark, m.Status)
		}
		return w.Flush()
	},
}

var embedModelRemoveCmd = &cobra.Command{
	Use:   "remove <slug>",
	Short: "Drop a model's vec table + delete its row (refuses default + shared)",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadAppFn()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.Registry == nil {
			return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
		}

		return a.Registry.Remove(cmd.Context(), args[0])
	},
}

// embedSwitchCmd flips is_default atomically to a registered model. It
// touches only the registry metadata — the new model's vec table stays
// empty until `embed reembed` runs. Prints a stderr reminder so users
// don't forget to migrate existing items.
var embedSwitchCmd = &cobra.Command{
	Use:   "switch",
	Short: "Set a registered model as the active default (atomic)",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadAppFn()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.Registry == nil {
			return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
		}

		slug := args[0]
		if err := a.Registry.SetDefault(cmd.Context(), slug); err != nil {
			return err
		}
		fmt.Fprintf(os.Stderr,
			"Active model switched to %s. Run 'unictx embed reembed' to migrate existing items.\n",
			slug)
		return nil
	},
}

// embedReembedCmd bulk-embeds items that lack a status='done' row for the
// active model. Mirrors embedBackfillCmd's shape: --limit caps the batch,
// --dry-run counts candidates without embedding. Ctrl+C (SIGINT/SIGTERM)
// cancels mid-batch via signalContext.
var embedReembedCmd = &cobra.Command{
	Use:   "reembed",
	Short: "Re-embed items lacking a done status row for the active model",
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadAppFn()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.Reembed == nil {
			return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
		}

		ctx := signalContext()
		report, err := a.Reembed.Run(ctx, reembedLimit, reembedDryRun)
		if err != nil {
			return err
		}

		if reembedDryRun {
			fmt.Printf("dry run: would re-embed %d items\n", report.Scanned)
			return nil
		}
		fmt.Printf("reembed complete: embedded=%d failed=%d scanned=%d\n",
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

// embedStatusCmd prints all context_embedding status rows for a given
// item, ordered by model_slug ASC. Read-only; safe to run anytime. Used
// to inspect per-model migration state during `embed switch` workflows.
// Plan 2c follow-up addition.
var embedStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show embedding status rows for an item (all models)",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadAppFn()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.EmbeddingRepo == nil {
			return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
		}

		rows, err := a.EmbeddingRepo.ListForItem(cmd.Context(), args[0])
		if err != nil {
			return err
		}
		if len(rows) == 0 {
			fmt.Printf("no embedding status rows for item %s\n", args[0])
			return nil
		}

		w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
		fmt.Fprintln(w, "MODEL_SLUG\tSTATUS\tATTEMPTS\tLAST_ERROR\tEMBEDDED_AT")
		for _, r := range rows {
			errCell := r.LastError
			if len(errCell) > 40 {
				errCell = errCell[:37] + "..."
			}
			fmt.Fprintf(w, "%s\t%s\t%d\t%s\t%d\n",
				r.ModelSlug, r.Status, r.Attempts, errCell, r.EmbeddedAt.Unix())
		}
		return w.Flush()
	},
}

func init() {
	embedBackfillCmd.Flags().IntVar(&backfillLimit, "limit", 0,
		"max items to embed (0 = no limit)")
	embedBackfillCmd.Flags().BoolVar(&backfillDryRun, "dry-run", false,
		"count candidates without embedding")
	embedWorkerCmd.Flags().DurationVar(&workerInterval, "interval", 30*time.Second,
		"poll interval for failed-embedding retries")
	embedReembedCmd.Flags().IntVar(&reembedLimit, "limit", 0,
		"max items to embed (0 = no limit)")
	embedReembedCmd.Flags().BoolVar(&reembedDryRun, "dry-run", false,
		"count candidates without embedding")

	embedModelAddCmd.Flags().StringVar(&modelAddProvider, "provider", "",
		"embedder provider (ollama|openai)")
	embedModelAddCmd.Flags().StringVar(&modelAddBaseURL, "base-url", "",
		"embedder base URL (e.g. http://localhost:11434 or https://api.openai.com/v1)")
	embedModelAddCmd.Flags().IntVar(&modelAddDim, "dim", 0,
		"embedding dimension (must match the model's output dim)")
	embedModelAddCmd.Flags().StringVar(&modelAddAPIKey, "api-key", "",
		"API key (required for OpenAI hosted; local servers ignore)")

	embedModelCmd.AddCommand(embedModelAddCmd)
	embedModelCmd.AddCommand(embedModelListCmd)
	embedModelCmd.AddCommand(embedModelRemoveCmd)

	embedCmd.AddCommand(embedBackfillCmd)
	embedCmd.AddCommand(embedWorkerCmd)
	embedCmd.AddCommand(embedModelCmd)   // Plan 2c
	embedCmd.AddCommand(embedSwitchCmd)  // Plan 2c
	embedCmd.AddCommand(embedReembedCmd) // Plan 2c
	embedCmd.AddCommand(embedStatusCmd)  // Plan 2c follow-up
	rootCmd.AddCommand(embedCmd)
}
