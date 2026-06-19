package config

import (
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

type Config struct {
	User    UserConfig `yaml:"user"`
	DataDir string     `yaml:"data_dir"`
}

type UserConfig struct {
	ID string `yaml:"id"`
}

// Load reads config from path (if it exists) and applies defaults.
// Missing file is not an error.
func Load(path string) (*Config, error) {
	cfg := &Config{
		User:    UserConfig{ID: "default"},
		DataDir: defaultDataDir(),
	}
	if data, err := os.ReadFile(path); err == nil {
		if err := yaml.Unmarshal(data, cfg); err != nil {
			return nil, err
		}
	}
	if cfg.DataDir == "" {
		cfg.DataDir = defaultDataDir()
	}
	if cfg.User.ID == "" {
		cfg.User.ID = "default"
	}
	return cfg, nil
}

func (c *Config) DBPath() string {
	return filepath.Join(c.DataDir, "unictx.db")
}

func (c *Config) FileStoreDir() string {
	return filepath.Join(c.DataDir, "filestore")
}

// DefaultConfigDir returns the user config dir (XDG-aware, falls back to ~/.config).
func DefaultConfigDir() string {
	if x := os.Getenv("XDG_CONFIG_HOME"); x != "" {
		return filepath.Join(x, "unictx")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	return filepath.Join(home, ".config", "unictx")
}

func defaultDataDir() string {
	if x := os.Getenv("XDG_DATA_HOME"); x != "" {
		return filepath.Join(x, "unictx")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	return filepath.Join(home, ".local", "share", "unictx")
}
