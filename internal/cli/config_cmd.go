package cli

import (
	"fmt"

	"uni-context/internal/config"

	"github.com/spf13/cobra"
)

var configCmd = &cobra.Command{
	Use:   "config",
	Short: "Inspect uni-context configuration",
}

var configPathCmd = &cobra.Command{
	Use:   "path",
	Short: "Print the config file path",
	RunE: func(cmd *cobra.Command, args []string) error {
		fmt.Println(flagConfigPath)
		return nil
	},
}

var configGetCmd = &cobra.Command{
	Use:   "get <key>",
	Short: "Get a config value (data_dir, db_path, filestore_dir)",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		cfg, err := config.Load(flagConfigPath)
		if err != nil {
			return err
		}
		switch args[0] {
		case "data_dir":
			fmt.Println(cfg.DataDir)
		case "db_path":
			fmt.Println(cfg.DBPath())
		case "filestore_dir":
			fmt.Println(cfg.FileStoreDir())
		case "user_id":
			fmt.Println(cfg.User.ID)
		default:
			return fmt.Errorf("unknown key %q (valid: data_dir, db_path, filestore_dir, user_id)", args[0])
		}
		return nil
	},
}

func init() {
	configCmd.AddCommand(configPathCmd, configGetCmd)
	rootCmd.AddCommand(configCmd)
}
