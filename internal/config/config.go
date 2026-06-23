package config

import (
	"errors"
	"fmt"
	"io/fs"
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

	// Provider selects the embedder adapter. Plan 2a shipped "ollama";
	// this patch adds "openai" for any OpenAI-compatible server
	// (LMStudio local, OpenAI hosted, vLLM, etc.).
	Provider string `yaml:"provider"` // "ollama" or "openai"

	// BaseURL is the API root. For Ollama, defaults to
	// http://localhost:11434. For OpenAI-compat, defaults to
	// http://localhost:1234/v1 (LMStudio's default port). OpenAI's
	// hosted API users should set this to https://api.openai.com/v1.
	// The OpenAI adapter appends "/embeddings" to this value.
	BaseURL string `yaml:"base_url"`

	Model     string `yaml:"model"`     // default "bge-m3"
	Dimension int    `yaml:"dimension"` // default 1024

	// APIKey is optional. Required for OpenAI's hosted API; local
	// servers (LMStudio, vLLM) typically ignore it. When empty, the
	// OpenAI adapter omits the Authorization header entirely.
	APIKey string `yaml:"api_key"`
}

// Load reads config from path (if it exists) and applies defaults.
// Missing file is not an error.
func Load(path string) (*Config, error) {
	cfg := &Config{
		User:    UserConfig{ID: "default"},
		DataDir: defaultDataDir(),
	}
	data, err := os.ReadFile(path)
	if err == nil {
		if err := yaml.Unmarshal(data, cfg); err != nil {
			return nil, err
		}
	} else if !errors.Is(err, fs.ErrNotExist) {
		return nil, fmt.Errorf("read config %s: %w", path, err)
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
		// BaseURL default is provider-specific: Ollama serves at
		// :11434, OpenAI-compat servers conventionally live at /v1
		// under whatever port (LMStudio's default is :1234).
		if cfg.Embedder.BaseURL == "" {
			switch cfg.Embedder.Provider {
			case "ollama":
				cfg.Embedder.BaseURL = "http://localhost:11434"
			case "openai":
				cfg.Embedder.BaseURL = "http://localhost:1234/v1"
			}
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
