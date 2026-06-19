//go:build integration

package ollama

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestEmbedder_Integration_RealOllama round-trips a real Ollama
// instance at OLLAMA_HOST (default http://localhost:11434). Requires `ollama
// pull bge-m3` first. Skipped if UNICTX_SKIP_OLLAMA=1 is set.
//
// OLLAMA_HOST handling: Ollama's native convention is host[:port] without
// scheme (e.g. "localhost:11434"). We accept either form — if no scheme is
// present we prepend "http://".
func TestEmbedder_Integration_RealOllama(t *testing.T) {
	if os.Getenv("UNICTX_SKIP_OLLAMA") == "1" {
		t.Skip("UNICTX_SKIP_OLLAMA=1")
	}
	host := os.Getenv("OLLAMA_HOST")
	if host == "" {
		host = "http://localhost:11434"
	} else if !strings.Contains(host, "://") {
		host = "http://" + host
	}

	e := New(host, "bge-m3", 1024)
	vecs, err := e.Embed(context.Background(), []string{"hello world", "你好世界"})
	require.NoError(t, err)
	require.Len(t, vecs, 2)
	require.Len(t, vecs[0], 1024, "bge-m3 must return 1024-dim vectors")
	assert.Len(t, vecs[1], 1024)
}
