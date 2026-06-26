package app

import (
	"bytes"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/config"
	"uni-context/internal/port"
)

func TestBuildPDFExtractor_NilWhenUnconfigured(t *testing.T) {
	ext, err := BuildPDFExtractor(config.PDFConfig{}, &bytes.Buffer{})
	require.NoError(t, err)
	assert.Nil(t, ext, "empty Engine means PDF disabled → (nil, nil)")
}

func TestBuildPDFExtractor_DefaultsToGxpdf(t *testing.T) {
	ext, err := BuildPDFExtractor(config.PDFConfig{Engine: "gxpdf"}, &bytes.Buffer{})
	require.NoError(t, err)
	require.NotNil(t, ext)
	// Don't assert concrete type — that couples the test to the impl.
	// Just verify it satisfies the port (the compile-time var in the
	// impl already does this, but a runtime check here is defensive).
	var _ port.PDFExtractor = ext
}

func TestBuildExtractorForEngine_ErrorsOnUnknownName(t *testing.T) {
	_, err := BuildExtractorForEngine("bogus", config.PDFConfig{}, &bytes.Buffer{})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "unknown pdf engine")
	assert.Contains(t, err.Error(), "bogus")
}

func TestBuildExtractorForEngine_ErrorsOnMissingShellConfig(t *testing.T) {
	// engine=shell but Engines map is nil → no command.
	_, err := BuildExtractorForEngine("shell", config.PDFConfig{}, &bytes.Buffer{})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "shell")
	assert.Contains(t, err.Error(), "command")
}

func TestBuildExtractorForEngine_ErrorsOnMissingHTTPConfig(t *testing.T) {
	_, err := BuildExtractorForEngine("http", config.PDFConfig{}, &bytes.Buffer{})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "http")
	assert.Contains(t, err.Error(), "url")
}

func TestBuildExtractorForEngine_ShellAppliesTimeoutDefault(t *testing.T) {
	cfg := config.PDFConfig{
		Engines: map[string]config.EngineConfig{
			"shell": {Command: "/bin/cat"}, // intentionally zero timeout
		},
	}
	ext, err := BuildExtractorForEngine("shell", cfg, &bytes.Buffer{})
	require.NoError(t, err)
	require.NotNil(t, ext)
	// We can't easily observe the timeout from outside. This test
	// exists mainly to assert the constructor doesn't panic with a
	// zero timeout. The ShellExtractor's own unit tests cover the
	// 30s default semantics.
}

func TestBuildExtractorForEngine_ShellUsesConfiguredCommand(t *testing.T) {
	cfg := config.PDFConfig{
		Engines: map[string]config.EngineConfig{
			"shell": {Command: "/bin/cat", Timeout: 5 * time.Second},
		},
	}
	ext, err := BuildExtractorForEngine("shell", cfg, &bytes.Buffer{})
	require.NoError(t, err)
	require.NotNil(t, ext)
	var _ port.PDFExtractor = ext
}
