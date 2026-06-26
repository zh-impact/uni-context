package config

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestLoad_DefaultsWhenNoFile(t *testing.T) {
	cfg, err := Load(filepath.Join(t.TempDir(), "nonexistent.yaml"))
	require.NoError(t, err)
	assert.Equal(t, "default", cfg.User.ID)
	assert.NotEmpty(t, cfg.DataDir)
	assert.NotEmpty(t, cfg.DBPath())
}

func TestLoad_ReadsYAML(t *testing.T) {
	dir := t.TempDir()
	yamlPath := filepath.Join(dir, "config.yaml")
	err := os.WriteFile(yamlPath, []byte(`
user:
  id: alice
data_dir: /tmp/custom-data
`), 0o644)
	require.NoError(t, err)

	cfg, err := Load(yamlPath)
	require.NoError(t, err)
	assert.Equal(t, "alice", cfg.User.ID)
	assert.Equal(t, "/tmp/custom-data", cfg.DataDir)
}

func TestConfig_DBPathDerivedFromDataDir(t *testing.T) {
	cfg := Config{DataDir: "/some/path"}
	assert.Equal(t, "/some/path/unictx.db", cfg.DBPath())
}

func TestLoad_EmbedderOllamaDefaults(t *testing.T) {
	dir := t.TempDir()
	yamlPath := filepath.Join(dir, "config.yaml")
	require.NoError(t, os.WriteFile(yamlPath, []byte(`
embedder:
  enabled: true
`), 0o644))

	cfg, err := Load(yamlPath)
	require.NoError(t, err)
	assert.True(t, cfg.Embedder.Enabled)
	assert.Equal(t, "ollama", cfg.Embedder.Provider)
	assert.Equal(t, "http://localhost:11434", cfg.Embedder.BaseURL)
	assert.Equal(t, "bge-m3", cfg.Embedder.Model)
	assert.Equal(t, 1024, cfg.Embedder.Dimension)
}

func TestLoad_EmbedderOpenAIDefaults(t *testing.T) {
	dir := t.TempDir()
	yamlPath := filepath.Join(dir, "config.yaml")
	require.NoError(t, os.WriteFile(yamlPath, []byte(`
embedder:
  enabled: true
  provider: openai
`), 0o644))

	cfg, err := Load(yamlPath)
	require.NoError(t, err)
	assert.True(t, cfg.Embedder.Enabled)
	// OpenAI-compat default targets LMStudio's local port (Plan 2d
	// preview: most users wiring provider=openai are local-server users).
	assert.Equal(t, "openai", cfg.Embedder.Provider)
	assert.Equal(t, "http://localhost:1234/v1", cfg.Embedder.BaseURL)
	assert.Equal(t, "bge-m3", cfg.Embedder.Model)
	assert.Equal(t, 1024, cfg.Embedder.Dimension)
}

func TestLoad_EmbedderExplicitValuesPreserved(t *testing.T) {
	dir := t.TempDir()
	yamlPath := filepath.Join(dir, "config.yaml")
	require.NoError(t, os.WriteFile(yamlPath, []byte(`
embedder:
  enabled: true
  provider: openai
  base_url: https://api.openai.com/v1
  model: text-embedding-3-small
  dimension: 1536
  api_key: sk-real-key
`), 0o644))

	cfg, err := Load(yamlPath)
	require.NoError(t, err)
	assert.Equal(t, "https://api.openai.com/v1", cfg.Embedder.BaseURL)
	assert.Equal(t, "text-embedding-3-small", cfg.Embedder.Model)
	assert.Equal(t, 1536, cfg.Embedder.Dimension)
	assert.Equal(t, "sk-real-key", cfg.Embedder.APIKey)
}

func TestLoad_EmbedderDisabledAppliesNoDefaults(t *testing.T) {
	// Plan 1 backward compat: when embedder.enabled is false (or absent),
	// Load must NOT populate provider/base_url/model/dimension — those
	// stay zero-valued so app.Wire's `if cfg.Embedder.Enabled` skips them.
	dir := t.TempDir()
	yamlPath := filepath.Join(dir, "config.yaml")
	require.NoError(t, os.WriteFile(yamlPath, []byte(`
embedder:
  enabled: false
`), 0o644))

	cfg, err := Load(yamlPath)
	require.NoError(t, err)
	assert.False(t, cfg.Embedder.Enabled)
	assert.Empty(t, cfg.Embedder.Provider)
	assert.Empty(t, cfg.Embedder.BaseURL)
	assert.Empty(t, cfg.Embedder.Model)
	assert.Equal(t, 0, cfg.Embedder.Dimension)
}

func TestLoad_PDFConfig(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	yaml := []byte(`
pdf:
  engine: shell
  engines:
    shell:
      command: 'pdftotext - -'
      timeout: 30s
    http:
      url: 'http://localhost:8000/extract'
      timeout: 45s
      auth_token: 'secret'
`)
	require.NoError(t, os.WriteFile(path, yaml, 0o600))
	cfg, err := Load(path)
	require.NoError(t, err)
	require.Equal(t, "shell", cfg.PDF.Engine)
	require.Len(t, cfg.PDF.Engines, 2)

	shell := cfg.PDF.Engines["shell"]
	assert.Equal(t, "pdftotext - -", shell.Command)
	assert.Equal(t, 30*time.Second, shell.Timeout)

	http := cfg.PDF.Engines["http"]
	assert.Equal(t, "http://localhost:8000/extract", http.URL)
	assert.Equal(t, 45*time.Second, http.Timeout)
	assert.Equal(t, "secret", http.AuthToken)
}

func TestLoad_PDFConfig_EmptyByDefault(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	require.NoError(t, os.WriteFile(path, []byte(`user: { id: test }`), 0o600))
	cfg, err := Load(path)
	require.NoError(t, err)
	assert.Equal(t, "", cfg.PDF.Engine, "PDF engine defaults to empty (disabled)")
	assert.Nil(t, cfg.PDF.Engines, "PDF engines map defaults to nil")
}
