package cli

import (
	"fmt"

	"github.com/spf13/cobra"
)

var doctorCmd = &cobra.Command{
	Use:   "doctor",
	Short: "Check that uni-context is set up correctly",
	RunE: func(cmd *cobra.Command, args []string) error {
		a, cfg, err := loadApp()
		if err != nil {
			return fmt.Errorf("setup error: %w", err)
		}
		defer a.Close()

		fmt.Printf("config path:    %s\n", flagConfigPath)
		fmt.Printf("data dir:       %s\n", cfg.DataDir)
		fmt.Printf("db path:        %s\n", cfg.DBPath())
		fmt.Printf("filestore dir:  %s\n", cfg.FileStoreDir())
		fmt.Printf("user id:        %s\n", cfg.User.ID)

		version, err := a.Diagnostics.SchemaVersion(cmd.Context())
		if err != nil {
			return fmt.Errorf("read schema version: %w", err)
		}
		fmt.Printf("schema version: %s\n", version)

		// Embedder check via DiagnosticService: when configured, exercise
		// the live service with a one-token embed. Otherwise report Plan 1
		// mode so users can see why hybrid search is unavailable. A failed
		// check flips the overall status to FAIL and surfaces a non-zero
		// exit so scripts and CI can detect the broken state.
		info, enabled, pingErr := a.Diagnostics.PingEmbedder(cmd.Context())
		var checkErr error
		if pingErr != nil {
			fmt.Printf("  embedder: FAIL (%v)\n", pingErr)
			checkErr = fmt.Errorf("embedder check failed: %w", pingErr)
		} else if enabled {
			fmt.Printf("  embedder: OK (%s, %d-dim)\n", info.Slug, info.Dimension)
		} else {
			fmt.Println("  embedder: disabled (Plan 1 mode; set embedder.enabled=true to enable)")
		}

		if checkErr != nil {
			fmt.Println("status:         FAIL (see above)")
			return checkErr
		}
		fmt.Println("status:         OK")
		return nil
	},
}

func init() {
	rootCmd.AddCommand(doctorCmd)
}
