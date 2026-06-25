package pdf

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// fixturePath resolves testdata/<name> relative to the test file.
// Tests run with cwd = package dir, so the path is just "testdata/<name>".
func fixturePath(t *testing.T, name string) string {
	t.Helper()
	p := filepath.Join("testdata", name)
	if _, err := os.Stat(p); err != nil {
		t.Fatalf("fixture %s missing: %v (regenerate per testdata/README.md)", p, err)
	}
	return p
}

func TestGxpdfExtractor_ExtractsKnownText(t *testing.T) {
	data, err := os.ReadFile(fixturePath(t, "sample.pdf"))
	require.NoError(t, err)
	x := NewGxpdfExtractor(&bytes.Buffer{})
	text, err := x.Extract(t.Context(), data)
	require.NoError(t, err)
	assert.Contains(t, text, "the quick brown fox",
		"extracted text must contain the body phrase")
}

func TestGxpdfExtractor_EmptyExtractionIsNotError(t *testing.T) {
	// Contract: image-only / blank PDFs return ("", nil), NOT an error.
	// The IngestService relies on this to distinguish "no text" from
	// "broken PDF" — the former stores the blob with empty Content,
	// the latter fails the entire Create call.
	data, err := os.ReadFile(fixturePath(t, "blank.pdf"))
	require.NoError(t, err)
	x := NewGxpdfExtractor(&bytes.Buffer{})
	text, err := x.Extract(t.Context(), data)
	require.NoError(t, err, "empty extraction must NOT be an error")
	assert.Empty(t, text, "blank PDF should yield no extractable text")
}

func TestGxpdfExtractor_EncryptedReturnsError(t *testing.T) {
	data, err := os.ReadFile(fixturePath(t, "encrypted.pdf"))
	require.NoError(t, err)
	x := NewGxpdfExtractor(&bytes.Buffer{})
	_, err = x.Extract(t.Context(), data)
	require.Error(t, err, "encrypted PDF without password must error")
	assert.Contains(t, err.Error(), "encrypted",
		"error message must include 'encrypted' so callers can message clearly")
}

func TestGxpdfExtractor_MalformedPDFReturnsError(t *testing.T) {
	x := NewGxpdfExtractor(&bytes.Buffer{})
	_, err := x.Extract(t.Context(), []byte("not a pdf at all"))
	require.Error(t, err)
}

// Verify the testdata dir is visible to go test (embedded FS sanity).
func TestTestDataFixturesExist(t *testing.T) {
	for _, name := range []string{"sample.pdf", "blank.pdf", "encrypted.pdf"} {
		_, err := os.ReadFile(filepath.Join("testdata", name))
		require.NoError(t, err, "fixture %s missing; see testdata/README.md", name)
	}
}
