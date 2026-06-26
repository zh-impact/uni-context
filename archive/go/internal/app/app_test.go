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

	assert.NotNil(t, a.embedder, "Embedder constructed when enabled")
	assert.NotNil(t, a.embRepo, "EmbeddingRepo constructed when enabled")
	assert.NotNil(t, a.Ingest, "IngestService constructed")
	assert.NotNil(t, a.Search, "SearchService constructed")
	// Plan 2b Task 5: Backfill now wired when embedder is enabled.
	assert.NotNil(t, a.Backfill, "Backfill constructed when embedder enabled")
	// Plan 2b Task 6: Worker now wired when embedder is enabled.
	assert.NotNil(t, a.Worker, "Worker constructed when embedder enabled")
}

func TestWire_EmbedderDisabled_LeavesEmbeddingFieldsNil(t *testing.T) {
	// Plan 1 compat: no embedder construction; App.Backfill/Worker/embedder
	// all nil so CLI commands error cleanly without nil-deref.
	dir := t.TempDir()
	cfg := &config.Config{DataDir: dir}

	a, err := Wire(cfg)
	require.NoError(t, err)
	t.Cleanup(func() { _ = a.Close() })

	assert.Nil(t, a.embedder)
	assert.Nil(t, a.embRepo)
	assert.Nil(t, a.Backfill)
	assert.Nil(t, a.Worker)
}

func TestWire_PDFEnabled_NoError(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		DataDir: dir,
		PDF:     config.PDFConfig{Engine: "gxpdf"},
	}
	a, err := Wire(cfg)
	require.NoError(t, err)
	t.Cleanup(func() { _ = a.Close() })
	// No public field exposes the extractor; the integration is verified
	// end-to-end by the CLI tests in Task 8. Here we just assert Wire
	// doesn't error when PDF is enabled with a valid engine.
}

func TestWire_PDFDisabled_NoError(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{DataDir: dir} // PDF zero-valued → Engine=""
	a, err := Wire(cfg)
	require.NoError(t, err)
	t.Cleanup(func() { _ = a.Close() })
}

func TestWire_PDFMisconfigured_Errors(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		DataDir: dir,
		PDF:     config.PDFConfig{Engine: "shell"}, // Engines map nil → no command
	}
	_, err := Wire(cfg)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "pdf")
}
