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

		var version string
		if err := a.DB.QueryRow(`SELECT value FROM schema_meta WHERE key='schema_version'`).Scan(&version); err != nil {
			return fmt.Errorf("read schema version: %w", err)
		}
		fmt.Printf("schema version: %s\n", version)

		// Embedder check: when configured, exercise the live service with
		// a one-token embed. Otherwise report Plan 1 mode so users can see
		// why hybrid search is unavailable. A failed check flips the
		// overall status to FAIL and surfaces a non-zero exit so scripts
		// and CI can detect the broken state.
		var checkErr error
		if a.Embedder != nil {
			_, err := a.Embedder.Embed(cmd.Context(), []string{"ping"})
			if err != nil {
				fmt.Printf("  embedder: FAIL (%v)\n", err)
				checkErr = fmt.Errorf("embedder check failed: %w", err)
			} else {
				info := a.Embedder.Model()
				fmt.Printf("  embedder: OK (%s, %d-dim)\n", info.Slug, info.Dimension)
			}
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
