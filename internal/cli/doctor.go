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
		defer a.DB.Close()

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
		fmt.Println("status:         OK")
		return nil
	},
}

func init() {
	rootCmd.AddCommand(doctorCmd)
}
