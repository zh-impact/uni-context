package cli

import (
	"fmt"
	"os"
	"path/filepath"

	"uni-context/internal/app"
	"uni-context/internal/config"

	"github.com/spf13/cobra"
)

var (
	flagConfigPath string
	flagJSON       bool
	flagVerbose    bool
)

var rootCmd = &cobra.Command{
	Use:   "unictx",
	Short: "Unified context knowledge management",
	PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
		if flagVerbose {
			// Future: configure slog to debug level
		}
		return nil
	},
}

func init() {
	rootCmd.PersistentFlags().StringVar(&flagConfigPath, "config",
		filepath.Join(config.DefaultConfigDir(), "config.yaml"),
		"path to config file")
	rootCmd.PersistentFlags().BoolVar(&flagJSON, "json", false, "output as JSON")
	rootCmd.PersistentFlags().BoolVar(&flagVerbose, "verbose", false, "verbose logging")
}

// Execute runs the root command.
func Execute() error {
	return rootCmd.Execute()
}

// SetVersion records the build version (called from main).
func SetVersion(v string) {
	rootCmd.Version = v
}

// loadApp is a helper for subcommands.
func loadApp() (*app.App, *config.Config, error) {
	cfg, err := config.Load(flagConfigPath)
	if err != nil {
		return nil, nil, fmt.Errorf("load config: %w", err)
	}
	a, err := app.Wire(cfg)
	if err != nil {
		return nil, cfg, err
	}
	return a, cfg, nil
}

// exitCode unwraps known errors and returns the CLI exit code.
func exitCode(err error) int {
	if err == nil {
		return 0
	}
	// simple classification for now
	return 1
}

func die(err error) {
	if err == nil {
		return
	}
	fmt.Fprintln(os.Stderr, "error:", err)
	os.Exit(exitCode(err))
}
