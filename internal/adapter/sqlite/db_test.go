package sqlite

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestOpen_TightensFilePermissionsTo0600 asserts that opening a file-backed
// DB tightens the on-disk mode to 0600 (or stricter). API keys now persist
// in embedding_model.config JSON, so the DB file must not be group/world
// readable. See plan-2c-final-fixes Fix #3.
func TestOpen_TightensFilePermissionsTo0600(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "test.db")

	// Create the file in advance with 0644 so we can prove Open tightens it.
	require.NoError(t, os.WriteFile(dbPath, []byte{}, 0o644))

	_, err := Open(dbPath)
	require.NoError(t, err)

	info, err := os.Stat(dbPath)
	require.NoError(t, err)
	mode := info.Mode().Perm()
	assert.Equal(t, os.FileMode(0o600), mode,
		"DB file mode must be tightened to 0600; got %o", mode)
}

// TestOpen_PreservesStricterMode makes sure Open does not relax an already
// 0600 file to something looser, and does not emit a spurious warning.
func TestOpen_PreservesStricterMode(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "strict.db")

	require.NoError(t, os.WriteFile(dbPath, []byte{}, 0o600))

	_, err := Open(dbPath)
	require.NoError(t, err)

	info, err := os.Stat(dbPath)
	require.NoError(t, err)
	assert.Equal(t, os.FileMode(0o600), info.Mode().Perm())
}

// TestOpen_MemoryDSNSkipsChmod asserts that opening :memory: does not
// create a file on disk. chmod is meaningless for in-memory DBs; Open
// must skip the tightening path entirely.
func TestOpen_MemoryDSNSkipsChmod(t *testing.T) {
	dir := t.TempDir()
	// chdir into TempDir so that any accidental file creation lands here
	// where we can detect it; restore cwd on cleanup.
	cwd, err := os.Getwd()
	require.NoError(t, err)
	require.NoError(t, os.Chdir(dir))
	t.Cleanup(func() { _ = os.Chdir(cwd) })

	db, err := Open(":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })

	entries, err := os.ReadDir(dir)
	require.NoError(t, err)
	for _, e := range entries {
		if e.Name() == ":memory:" {
			t.Fatalf("Open(':memory:') created a literal file named ':memory:'")
		}
	}
	assert.Empty(t, entries, "no files should be created for :memory: DSN")
}
