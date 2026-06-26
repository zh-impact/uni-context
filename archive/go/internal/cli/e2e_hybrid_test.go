//go:build integration && e2e

package cli

import (
	"bytes"
	"encoding/json"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestE2E_HybridSearch exercises the full Plan 2a vertical slice with a
// real Ollama backend: write a config with embedder.enabled=true, ingest
// two notes (one of which embeds to a vector near the query), then run
// `search --mode hybrid` and confirm MatchedBy reports fts+vector for the
// semantically-relevant hit.
//
// The test is gated twice to keep it out of the default suite:
//  1. //go:build integration — only runs under `make test-integration`.
//  2. UNICTX_E2E_HYBRID=1    — opt-in even within integration runs.
//
// It also pings Ollama up front; if the service or the bge-m3 model is
// unavailable, the test skips rather than fails, so CI without a GPU
// can still run integration tests cleanly.
func TestE2E_HybridSearch(t *testing.T) {
	if os.Getenv("UNICTX_E2E_HYBRID") != "1" {
		t.Skip("UNICTX_E2E_HYBRID=1 not set; skipping hybrid e2e test")
	}

	baseURL := os.Getenv("OLLAMA_BASE_URL")
	if baseURL == "" {
		baseURL = "http://localhost:11434"
	}
	if !ollamaReachable(t, baseURL) {
		t.Skipf("Ollama unreachable at %s; skipping hybrid e2e test", baseURL)
	}

	bin := binPath(t)
	home := t.TempDir()

	// Write a config with embedder.enabled=true. Defaults provider=ollama,
	// base_url, model=bge-m3, dimension=1024 — applied by config.Load.
	cfgDir := filepath.Join(home, ".config", "unictx")
	require.NoError(t, os.MkdirAll(cfgDir, 0o755))
	cfgPath := filepath.Join(cfgDir, "config.yaml")
	cfgBody := []byte("user:\n  id: e2e\nembedder:\n  enabled: true\n")
	require.NoError(t, os.WriteFile(cfgPath, cfgBody, 0o600))

	runHybrid := func(args ...string) (string, int) {
		t.Helper()
		full := append([]string{"--config", cfgPath}, args...)
		cmd := exec.Command(bin, full...)
		cmd.Env = append(os.Environ(),
			"HOME="+home,
			"XDG_DATA_HOME="+filepath.Join(home, ".local", "share"))
		var out, errBuf bytes.Buffer
		cmd.Stdout = &out
		cmd.Stderr = &errBuf
		err := cmd.Run()
		exit := 0
		if err != nil {
			if ee, ok := err.(*exec.ExitError); ok {
				exit = ee.ExitCode()
			} else {
				t.Fatalf("spawn %v: %v", full, err)
			}
		}
		if exit != 0 {
			t.Logf("stderr: %s", errBuf.String())
		}
		return out.String(), exit
	}

	// doctor must reflect that the embedder is wired and reachable.
	_, code := runHybrid("doctor")
	require.Zero(t, code, "doctor should succeed when embedder is reachable")

	// Ingest: first note is the semantic match (no lexical overlap with
	// the query), second note is a distractor with lexical overlap.
	// Semantic-similarity test would be flaky with a single sample, so
	// we assert the weaker but stable property: hybrid mode runs end-to-end
	// and returns at least one fts+vector hit when both corpora match.
	_, code = runHybrid("user", "note", "add",
		"Kubernetes deployment rolling update strategy",
		"--title", "k8s deploy", "--json")
	require.Zero(t, code, "ingest of semantic note should succeed")

	_, code = runHybrid("user", "note", "add",
		"deploy k8s cluster",
		"--title", "k8s quickstart", "--json")
	require.Zero(t, code, "ingest of distractor note should succeed")

	out, code := runHybrid("search", "deploy k8s", "--mode", "hybrid", "--limit", "5", "--json")
	require.Zero(t, code, "hybrid search should succeed")

	var resp struct {
		Results []struct {
			ID        string   `json:"id"`
			MatchedBy []string `json:"matched_by"`
		} `json:"results"`
		Total int    `json:"total"`
		Mode  string `json:"mode"`
	}
	require.NoError(t, json.Unmarshal([]byte(out), &resp))
	assert.Equal(t, "hybrid", resp.Mode, "JSON mode field must echo --mode")
	assert.GreaterOrEqual(t, resp.Total, 1, "at least one match expected")

	// At least one result should be hit by both FTS and vector when the
	// embedder is wired. If this assertion ever flakes, it means the model
	// produced distant embeddings — investigate before silencing.
	hasBoth := false
	for _, r := range resp.Results {
		if containsAll(r.MatchedBy, "fts", "vector") {
			hasBoth = true
			break
		}
	}
	assert.True(t, hasBoth, "expected at least one result matched by fts+vector; got %+v", resp.Results)
}

// ollamaReachable pings the Ollama root endpoint with a short timeout.
// We don't check for the bge-m3 model here — a missing model surfaces as
// a clear error from `doctor` / `search`, which is more useful for
// debugging than a skip.
func ollamaReachable(t *testing.T, baseURL string) bool {
	t.Helper()
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(baseURL + "/")
	if err != nil {
		return false
	}
	_ = resp.Body.Close()
	return true
}

// containsAll reports whether all needles appear in haystack. Order
// independent; matches the MatchedBy semantics where the service emits
// ["fts","vector"] (or any subset).
func containsAll(haystack []string, needles ...string) bool {
	for _, n := range needles {
		found := false
		for _, h := range haystack {
			if h == n {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}
