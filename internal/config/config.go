package config

import (
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

type Config struct {
	User     UserConfig     `yaml:"user"`
	DataDir  string         `yaml:"data_dir"`
	Embedder EmbedderConfig `yaml:"embedder"`
}

type UserConfig struct {
	ID string `yaml:"id"`
}

// EmbedderConfig controls the optional embedding pipeline (Plan 2a).
// When Enabled is false (the default), the app behaves exactly as Plan 1
// (no vector indexing, search defaults to fts-only). When Enabled is true,
// the defaults below are applied to any zero-valued fields, and app.Wire
// constructs the configured embedder.
type EmbedderConfig struct {
	// Enabled controls whether ingest triggers embedding and search
	// supports hybrid mode. Default false (Plan 1 compat).
	Enabled bool `yaml:"enabled"`

	Provider  string `yaml:"provider"`  // "ollama" (only option in 2a)
	BaseURL   string `yaml:"base_url"`  // default http://localhost:11434
	Model     string `yaml:"model"`     // default "bge-m3"
	Dimension int    `yaml:"dimension"` // default 1024
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
	if cfg.Embedder.Enabled {
		if cfg.Embedder.Provider == "" {
			cfg.Embedder.Provider = "ollama"
		}
		if cfg.Embedder.BaseURL == "" {
			cfg.Embedder.BaseURL = "http://localhost:11434"
		}
		if cfg.Embedder.Model == "" {
			cfg.Embedder.Model = "bge-m3"
		}
		if cfg.Embedder.Dimension == 0 {
			cfg.Embedder.Dimension = 1024
		}
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
