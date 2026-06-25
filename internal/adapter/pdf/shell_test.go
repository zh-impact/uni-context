package pdf

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// writeScript creates an executable temp file whose body is script.
// Returns the absolute path. Skips the test on OSes where chmod +x
// isn't meaningful (Windows).
func writeScript(t *testing.T, script string) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("shell extractor tests rely on chmod +x; skip on Windows")
	}
	f, err := os.CreateTemp("", "shell-ext-*")
	require.NoError(t, err)
	defer f.Close()
	_, err = f.WriteString(script)
	require.NoError(t, err)
	require.NoError(t, f.Chmod(0o755))
	abs, err := filepath.Abs(f.Name())
	require.NoError(t, err)
	return abs
}

func TestShellExtractor_ExtractsStdout(t *testing.T) {
	// Stub script that prints canned text to stdout and exits 0.
	stub := writeScript(t, "#!/bin/sh\necho 'canned extracted text'\n")
	x := NewShellExtractor(stub, 5*time.Second)

	text, err := x.Extract(context.Background(), []byte("%PDF-1.4 fake"))
	require.NoError(t, err)
	assert.Equal(t, strings.TrimSpace(text), "canned extracted text")
}

func TestShellExtractor_PropagatesNonZeroExit(t *testing.T) {
	stub := writeScript(t, "#!/bin/sh\necho 'parse failed' 1>&2\nexit 3\n")
	x := NewShellExtractor(stub, 5*time.Second)

	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	s := err.Error()
	assert.Contains(t, s, "exit 3", "error must mention exit code")
	assert.Contains(t, s, "parse failed", "error must include stderr")
}

func TestShellExtractor_TimesOut(t *testing.T) {
	stub := writeScript(t, "#!/bin/sh\nsleep 5\n")
	x := NewShellExtractor(stub, 100*time.Millisecond)

	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "timeout", "error must mention timeout")
}

func TestShellExtractor_BinaryNotFound(t *testing.T) {
	x := NewShellExtractor("/nonexistent/path/definitely-not-here", 5*time.Second)
	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "not found",
		"error must mention the binary is missing")
}
