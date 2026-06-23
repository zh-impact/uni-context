package cli

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/app"
	"uni-context/internal/config"
	"uni-context/internal/domain"
	"uni-context/internal/port"
	"uni-context/internal/service"
)

// swapUserNoteLoadAppFn swaps the package-level userNoteLoadAppFn to return
// a stubbed *App with a non-empty User.ID (ScopeUser + empty owner fails
// domain validation). Returns a restore func — tests MUST defer it.
// Separate from swapLoadAppFn in embed_status_test.go so embed RunE tests
// and userNote RunE tests each swap their own var without interference.
func swapUserNoteLoadAppFn(a *app.App) func() {
	prev := userNoteLoadAppFn
	userNoteLoadAppFn = func() (*app.App, *config.Config, error) {
		return a, &config.Config{User: config.UserConfig{ID: "test-user"}}, nil
	}
	return func() { userNoteLoadAppFn = prev }
}

// capturingRepo stores every Create'd item so RunE tests can assert on
// the Input the RunE constructed (MIME, SourceMeta, Content, etc.).
// Other methods panic — RunE tests only exercise the Create path.
type capturingRepo struct {
	created []domain.ContextItem
}

func (r *capturingRepo) Create(_ context.Context, item domain.ContextItem) error {
	r.created = append(r.created, item)
	return nil
}
func (r *capturingRepo) Get(_ context.Context, id string) (domain.ContextItem, error) {
	panic("unexpected Get call in RunE test")
}
func (r *capturingRepo) Update(_ context.Context, item domain.ContextItem) (domain.ContextItem, error) {
	panic("unexpected Update call in RunE test")
}
func (r *capturingRepo) Delete(_ context.Context, id string) error {
	panic("unexpected Delete call in RunE test")
}
func (r *capturingRepo) List(_ context.Context, _ port.ItemFilter) ([]domain.ContextItem, string, error) {
	panic("unexpected List call in RunE test")
}
func (r *capturingRepo) NextCursor(_ domain.ContextItem) string { panic("unexpected NextCursor") }

// resetNoteFlags zeroes all package-level flag vars that userNoteAddCmd
// reads. Call at the start of each RunE test and via t.Cleanup so package
// state doesn't leak between tests.
func resetNoteFlags(t *testing.T) {
	t.Helper()
	noteFilePath = ""
	noteTitle = ""
	noteTags = nil
	flagJSON = false
	t.Cleanup(func() {
		noteFilePath = ""
		noteTitle = ""
		noteTags = nil
		flagJSON = false
	})
}

// TestUserNoteAddCmd_RunEWithFileImport_PreservesFilenameAndMIME: importing
// a small .md file must produce an item with ContentMIME="text/markdown",
// SourceMeta["original_filename"] set, content inline (not externalized),
// and a title derived from the basename. Exercises the full CLI → service
// path with a real IngestService against a capturing repo.
func TestUserNoteAddCmd_RunEWithFileImport_PreservesFilenameAndMIME(t *testing.T) {
	repo := &capturingRepo{}
	a := newStubApp(t)
	// emptyFileStore is reused from embed_run_e_test.go — safe because the
	// small fixture stays inline and never calls fs.Put.
	a.Ingest = service.NewIngestService(repo, emptyFileStore{})
	restore := swapUserNoteLoadAppFn(a)
	defer restore()
	resetNoteFlags(t)

	dir := t.TempDir()
	path := filepath.Join(dir, "weekly.md")
	require.NoError(t, os.WriteFile(path, []byte("# weekly notes"), 0o644))

	rootCmd.SetArgs([]string{"user", "note", "add", "--file", path, "--tag", "work"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	require.NoError(t, rootCmd.Execute())

	require.Len(t, repo.created, 1, "exactly one item should be created")
	item := repo.created[0]
	assert.Equal(t, "weekly.md", item.SourceMeta["original_filename"],
		"original filename must be preserved in SourceMeta")
	assert.Equal(t, "text/markdown", item.ContentMIME,
		"MIME must be detected from .md extension and preserved on inline item")
	assert.Equal(t, "# weekly notes", item.Content,
		"small file content stays inline")
	assert.Empty(t, item.ContentURI, "small file should not be externalized")
	assert.Equal(t, "weekly", item.Title,
		"title should derive from basename with extension stripped")
	assert.Equal(t, []string{"work"}, item.Tags)
}

// TestUserNoteAddCmd_RunEFileFlagMutuallyExclusiveWithPositional: passing
// both --file and a positional arg must error cleanly with the "cannot
// combine" message. This is Rule 1 from the spec.
func TestUserNoteAddCmd_RunEFileFlagMutuallyExclusiveWithPositional(t *testing.T) {
	repo := &capturingRepo{}
	a := newStubApp(t)
	a.Ingest = service.NewIngestService(repo, emptyFileStore{})
	restore := swapUserNoteLoadAppFn(a)
	defer restore()
	resetNoteFlags(t)

	dir := t.TempDir()
	path := filepath.Join(dir, "x.txt")
	require.NoError(t, os.WriteFile(path, []byte("x"), 0o644))

	rootCmd.SetArgs([]string{"user", "note", "add", "--file", path, "extra-positional"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	err := rootCmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "cannot combine --file")
	assert.Empty(t, repo.created, "no item should be created on validation failure")
}

// TestUserNoteAddCmd_RunEPropagatesValidationError: pointing --file at a
// nonexistent path must surface the validator's "stat file:" error. Zero
// bytes written to disk. Confirms RunE invokes validateFileImport and
// propagates its error.
func TestUserNoteAddCmd_RunEPropagatesValidationError(t *testing.T) {
	repo := &capturingRepo{}
	a := newStubApp(t)
	a.Ingest = service.NewIngestService(repo, emptyFileStore{})
	restore := swapUserNoteLoadAppFn(a)
	defer restore()
	resetNoteFlags(t)

	missingPath := filepath.Join(t.TempDir(), "does-not-exist.txt")
	rootCmd.SetArgs([]string{"user", "note", "add", "--file", missingPath})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	err := rootCmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "stat file:")
	assert.Empty(t, repo.created)
}
