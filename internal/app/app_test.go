package app

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/config"
)

func TestWire_EmbedderEnabled_ConstructsEmbeddingRepo(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		DataDir: dir,
		Embedder: config.EmbedderConfig{
			Enabled:   true,
			Provider:  "ollama",
			BaseURL:   "http://127.0.0.1:65535", // closed port; Wire does not call the embedder
			Model:     "bge-m3",
			Dimension: 1024,
		},
	}

	a, err := Wire(cfg)
	require.NoError(t, err)
	t.Cleanup(func() { _ = a.Close() })

	assert.NotNil(t, a.Embedder, "Embedder constructed when enabled")
	assert.NotNil(t, a.EmbeddingRepo, "EmbeddingRepo constructed when enabled")
	assert.NotNil(t, a.Ingest, "IngestService constructed")
	assert.NotNil(t, a.Search, "SearchService constructed")
	// Plan 2b Task 5: Backfill now wired when embedder is enabled.
	assert.NotNil(t, a.Backfill, "Backfill constructed when embedder enabled")
	// Plan 2b Task 6: Worker now wired when embedder is enabled.
	assert.NotNil(t, a.Worker, "Worker constructed when embedder enabled")
}

func TestWire_EmbedderDisabled_LeavesEmbeddingFieldsNil(t *testing.T) {
	// Plan 1 compat: no embedder construction; App.Backfill/Worker/Embedder
	// all nil so CLI commands error cleanly without nil-deref.
	dir := t.TempDir()
	cfg := &config.Config{DataDir: dir}

	a, err := Wire(cfg)
	require.NoError(t, err)
	t.Cleanup(func() { _ = a.Close() })

	assert.Nil(t, a.Embedder)
	assert.Nil(t, a.EmbeddingRepo)
	assert.Nil(t, a.Backfill)
	assert.Nil(t, a.Worker)
}
