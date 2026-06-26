//go:build integration && e2e

package cli

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestE2E_BackfillRecoversFromIngestFailure proves the full Plan 2b
// recovery path end-to-end against a real embedder:
//  1. Ingest 3 items while the embedder is unreachable (bogus base_url).
//     Each ingest must still succeed (embedding failure is non-fatal),
//     and each must leave a status='failed' row in context_embedding.
//  2. Swap the config to point at a live embedder.
//  3. Run `embed worker` briefly. It polls ListFailed and retries each
//     row via EmbedService.Embed, flipping status to 'done' on success.
//  4. Assert all 3 rows reached status='done'.
//
// The test is gated twice so it never runs in the default suite:
//   - //go:build integration && e2e — only compiled under the
//     integration+e2e tag combo (CI's test-integration target, or a
//     manual `go test -tags 'sqlite_fts5,integration,e2e'`).
//   - UNICTX_E2E_BACKFILL=1 — env-gate so the test is still skipped
//     even when the tags are active, unless the caller explicitly opts
//     in. This matches the e2e_hybrid_test.go pattern.
//
// Required setup to actually run (rather than skip):
//   - A built unictx binary (run `make build` from the repo root first,
//     or set UNICTX_BIN to an absolute path).
//   - A reachable Ollama (or OpenAI-compat) server with bge-m3 pulled.
//     Override the default http://localhost:11434 by setting
//     UNICTX_E2E_EMBEDDER_URL.
func TestE2E_BackfillRecoversFromIngestFailure(t *testing.T) {
	if os.Getenv("UNICTX_E2E_BACKFILL") != "1" {
		t.Skip("set UNICTX_E2E_BACKFILL=1 + provide a live embedder to run")
	}

	bin := binPath(t)
	home := t.TempDir()

	// Config path — we use --config explicitly (same pattern as
	// e2e_hybrid_test.go) so the test does not depend on XDG env vars
	// lining up with whatever the ambient shell has set.
	cfgDir := filepath.Join(home, "config", "unictx")
	require.NoError(t, os.MkdirAll(cfgDir, 0o755))
	cfgPath := filepath.Join(cfgDir, "config.yaml")

	// Phase 1 config: embedder enabled but pointed at a dead port.
	// Port 65535 is in the reserved-and-unreachable range, so the HTTP
	// client fails fast with connection-refused. Provider is openai
	// because Ollama's adapter retries differently; we want the raw
	// "embedder down" semantics.
	cfgBad := strings.Join([]string{
		"user:",
		"  id: e2e",
		"embedder:",
		"  enabled: true",
		"  provider: openai",
		`  base_url: http://127.0.0.1:65535/v1`,
		"  model: bge-m3",
		"  dimension: 1024",
		"",
	}, "\n")
	require.NoError(t, os.WriteFile(cfgPath, []byte(cfgBad), 0o600))

	// runIngest runs the binary with the given args and shares the
	// home dir layout (XDG_DATA_HOME under home/data so the DB lands
	// somewhere we can inspect with sqlite3).
	runIngest := func(args ...string) string {
		t.Helper()
		full := append([]string{"--config", cfgPath}, args...)
		cmd := exec.Command(bin, full...)
		cmd.Env = append(os.Environ(),
			"XDG_DATA_HOME="+filepath.Join(home, "data"))
		out, err := cmd.CombinedOutput()
		require.NoErrorf(t, err, "run %v: %s", full, out)
		return string(out)
	}

	// Phase 1: ingest 3 items against the broken embedder. Each add
	// must succeed at the persistence layer; embedding failure is
	// non-fatal by design (Plan 2a contract preserved in 2b).
	for _, title := range []string{"alpha", "beta", "gamma"} {
		out := runIngest("user", "note", "add", "content "+title, "--title", title)
		assert.Contains(t, out, "added",
			"ingest should succeed even when embed fails (got: %q)", out)
	}

	// Verify 3 status='failed' rows landed in context_embedding. This
	// is the core Plan 2b guarantee: every embed attempt writes a row.
	dbPath := filepath.Join(home, "data", "unictx", "unictx.db")
	require.Equal(t, 3, queryCount(t, dbPath,
		`SELECT count(*) FROM context_embedding WHERE status='failed'`),
		"expected 3 failed status rows after broken-ingest phase")

	// Phase 2: rewrite config with the REAL embedder URL. The default
	// matches Ollama; override via UNICTX_E2E_EMBEDDER_URL to target
	// LMStudio or another OpenAI-compat server.
	realURL := os.Getenv("UNICTX_E2E_EMBEDDER_URL")
	if realURL == "" {
		realURL = "http://localhost:11434"
	}
	cfgGood := strings.Replace(cfgBad, "http://127.0.0.1:65535/v1", realURL, 1)
	require.NoError(t, os.WriteFile(cfgPath, []byte(cfgGood), 0o600))

	// Phase 3: run the worker long enough to process 3 rows. The worker
	// polls at --interval and would run forever; we let it tick once
	// or twice, then SIGTERM it. signalContext in embed.go treats
	// SIGTERM as graceful shutdown, so this is the same code path a
	// human Ctrl+C would take.
	workerCmd := exec.Command(bin, "--config", cfgPath,
		"embed", "worker", "--interval", "1s")
	workerCmd.Env = append(os.Environ(),
		"XDG_DATA_HOME="+filepath.Join(home, "data"))
	require.NoError(t, workerCmd.Start())

	done := make(chan error, 1)
	go func() { done <- workerCmd.Wait() }()
	select {
	case <-time.After(5 * time.Second):
		// Time's up — kill the worker. SIGINT first for graceful path
		// (worker subscribes to both SIGINT and SIGTERM); if that doesn't
		// land, the test still asserts on DB state.
		_ = workerCmd.Process.Signal(os.Interrupt)
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			_ = workerCmd.Process.Kill()
		}
	case <-done:
	}

	// Phase 4: all 3 rows must have flipped to status='done'. If this
	// flakes, it usually means the embedder was slow / the model wasn't
	// pulled / the URL was wrong — recheck UNICTX_E2E_EMBEDDER_URL and
	// `ollama list` before assuming a regression.
	got := queryCount(t, dbPath,
		`SELECT count(*) FROM context_embedding WHERE status='done'`)
	assert.Equal(t, 3, got,
		"all 3 items should be status='done' after worker run "+
			"(failed=%d done=%d)",
		queryCount(t, dbPath, `SELECT count(*) FROM context_embedding WHERE status='failed'`),
		got,
	)
}

// queryCount shells out to the sqlite3 CLI to read a single integer
// scalar from the test's DB. Using the CLI (rather than a Go driver)
// keeps the e2e test free of CGO/driver setup — the binary under test
// holds the write lock during its run, and by the time we query, the
// process has exited and the file is quiescent.
func queryCount(t *testing.T, dbPath, query string) int {
	t.Helper()
	out, err := exec.Command("sqlite3", dbPath, query).Output()
	require.NoErrorf(t, err, "sqlite3 query failed: %q\noutput: %s", query, out)
	var n int
	_, err = fmt.Sscanf(strings.TrimSpace(string(out)), "%d", &n)
	require.NoErrorf(t, err, "parse sqlite3 output %q as int", out)
	return n
}
