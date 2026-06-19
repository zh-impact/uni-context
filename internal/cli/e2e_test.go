//go:build e2e

package cli

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// binPath returns the path to the built unictx binary.
// The CI workflow builds it before running tests; for local runs you can
// `make build` first.
func binPath(t *testing.T) string {
	t.Helper()
	candidates := []string{
		os.Getenv("UNICTX_BIN"),
		"../../unictx",
		"../../dist/unictx",
	}
	for _, c := range candidates {
		if c == "" {
			continue
		}
		if _, err := os.Stat(c); err == nil {
			return c
		}
	}
	t.Skip("unictx binary not built; run `make build` first")
	return ""
}

func run(t *testing.T, home string, args ...string) (string, int) {
	t.Helper()
	cmd := exec.Command(binPath(t), args...)
	cmd.Env = append(os.Environ(), "HOME="+home, "XDG_DATA_HOME="+filepath.Join(home, ".local", "share"))
	var out, errBuf bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errBuf
	err := cmd.Run()
	exitCode := 0
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			exitCode = ee.ExitCode()
		} else {
			t.Fatalf("spawn binary: %v", err)
		}
	}
	if exitCode != 0 && testing.Verbose() {
		t.Logf("stderr: %s", errBuf.String())
	}
	return out.String(), exitCode
}

func TestE2E_NoteLifecycleAndSearch(t *testing.T) {
	home := t.TempDir()

	// Sanity: doctor works on fresh state
	_, code := run(t, home, "doctor")
	require.Zero(t, code, "doctor should succeed on fresh state")

	// Add note A
	outA, code := run(t, home, "user", "note", "add", "How to deploy Go services",
		"--title", "Deploy Guide", "--tag", "go", "--tag", "deploy", "--json")
	require.Zero(t, code)
	var respA struct {
		ID string `json:"id"`
	}
	require.NoError(t, json.Unmarshal([]byte(outA), &respA))
	require.NotEmpty(t, respA.ID)

	// Add note B
	_, code = run(t, home, "user", "note", "add", "Python scraping tutorial",
		"--title", "Scraping", "--tag", "python")
	require.Zero(t, code)

	// List should show 2
	outList, code := run(t, home, "user", "note", "list", "--json")
	require.Zero(t, code)
	var listResp []map[string]any
	require.NoError(t, json.Unmarshal([]byte(outList), &listResp))
	assert.Len(t, listResp, 2)

	// Search "deploy" should find note A
	outSearch, code := run(t, home, "search", "deploy", "--json")
	require.Zero(t, code)
	var searchResp struct {
		Results []map[string]any `json:"results"`
		Total   int              `json:"total"`
	}
	require.NoError(t, json.Unmarshal([]byte(outSearch), &searchResp))
	assert.GreaterOrEqual(t, searchResp.Total, 1)
	assert.Equal(t, respA.ID, searchResp.Results[0]["id"])

	// Get the note
	outGet, code := run(t, home, "user", "note", "get", respA.ID, "--json")
	require.Zero(t, code)
	var getResp map[string]any
	require.NoError(t, json.Unmarshal([]byte(outGet), &getResp))
	assert.Equal(t, "Deploy Guide", getResp["title"])

	// Delete
	_, code = run(t, home, "user", "note", "delete", respA.ID, "--json")
	require.Zero(t, code)

	// Search should now miss it
	outSearch2, _ := run(t, home, "search", "deploy", "--json")
	var searchResp2 struct {
		Total int `json:"total"`
	}
	_ = json.Unmarshal([]byte(outSearch2), &searchResp2)
	assert.Equal(t, 0, searchResp2.Total, "deleted note should not match")
}

func TestE2E_LargeContentExternalized(t *testing.T) {
	home := t.TempDir()
	big := strings.Repeat("long content word ", 500) // ~9KB

	// Pass via stdin
	cmd := exec.Command(binPath(t), "user", "note", "add", "-", "--title", "Big", "--json")
	cmd.Env = append(os.Environ(), "HOME="+home,
		"XDG_DATA_HOME="+filepath.Join(home, ".local", "share"))
	cmd.Stdin = strings.NewReader(big)
	var out bytes.Buffer
	cmd.Stdout = &out
	require.NoError(t, cmd.Run())

	var resp struct {
		ID string `json:"id"`
	}
	require.NoError(t, json.Unmarshal(out.Bytes(), &resp))

	// get should return full content
	outGet, code := run(t, home, "user", "note", "get", resp.ID, "--json")
	require.Zero(t, code)
	var getResp map[string]any
	require.NoError(t, json.Unmarshal([]byte(outGet), &getResp))
	assert.Equal(t, big, getResp["content"])
}
