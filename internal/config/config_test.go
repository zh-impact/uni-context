package config

import (
	"os"
	"path/filepath"
	"testing"

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
