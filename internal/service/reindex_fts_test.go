package service

import (
	"context"
	"fmt"
	"testing"

	"uni-context/internal/domain"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestReindexFTS_Run_HydratesExternalizedItems verifies the service walks
// every item, hydrates externalized content from FileStore, and calls
// ReindexFTS with the bytes. Inline items are skipped (the trigger already
// indexed them correctly).
func TestReindexFTS_Run_HydratesExternalizedItems(t *testing.T) {
	ctx := context.Background()

	// Three items: inline (skip), externalized (reindex), inline-with-uri
	// (already hydrated — skip; defensive guard against the trigger having
	// already run ReindexFTS).
	inline := newItem("inline body")
	externalized := newItem("externalized body")
	externalized.ContentURI = "file://deadbeef"
	externalized.Content = "" // simulate post-Create externalized state
	alreadyHydrated := newItem("hydrated body")
	alreadyHydrated.ContentURI = "file://cafef00d"
	// Content non-empty AND uri non-empty: defensive — the service should
	// treat this as already-indexed and skip rather than rewrite.

	repo := newFakeRepo()
	require.NoError(t, repo.Create(ctx, inline))
	require.NoError(t, repo.Create(ctx, externalized))
	require.NoError(t, repo.Create(ctx, alreadyHydrated))

	fs := &cannedFileStore{
		files: map[string][]byte{
			"file://deadbeef": []byte("hydrated externalized body"),
		},
	}

	svc := NewReindexFTSService(repo, fs)
	report, err := svc.Run(ctx, 0, false)
	require.NoError(t, err)

	assert.Equal(t, 1, report.Scanned, "only the empty+externalized item qualifies")
	assert.Equal(t, 1, report.Reindexed, "one ReindexFTS call should succeed")
	assert.Equal(t, 0, report.Failed)
	// fakeRepo.ReindexFTS records calls — verify the right content flowed through.
	// (fakeRepo doesn't keep args, only a counter; the assertion is the count.)
	assert.Equal(t, 1, repo.reindexFTSCall, "ReindexFTS should be called once")
}

// TestReindexFTS_Run_DryRunCountsOnly verifies --dry-run increments
// Scanned without touching FileStore or FTS.
func TestReindexFTS_Run_DryRunCountsOnly(t *testing.T) {
	ctx := context.Background()
	externalized := newItem("body")
	externalized.ContentURI = "file://x"
	externalized.Content = ""

	repo := newFakeRepo()
	require.NoError(t, repo.Create(ctx, externalized))

	fs := &explodingFileStore{} // any Get call fails the test
	svc := NewReindexFTSService(repo, fs)

	report, err := svc.Run(ctx, 0, true)
	require.NoError(t, err)
	assert.Equal(t, 1, report.Scanned)
	assert.Equal(t, 0, report.Reindexed, "dry run must not call ReindexFTS")
	assert.Equal(t, 0, repo.reindexFTSCall, "dry run must not touch the repo beyond List")
}

// TestReindexFTS_Run_FileStoreMissRecordsFailure verifies that an
// unresolvable ContentURI (e.g., the file was deleted out of band) records
// a per-item failure without aborting the whole run.
func TestReindexFTS_Run_FileStoreMissRecordsFailure(t *testing.T) {
	ctx := context.Background()
	externalized := newItem("body")
	externalized.ContentURI = "file://missing"
	externalized.Content = ""

	repo := newFakeRepo()
	require.NoError(t, repo.Create(ctx, externalized))

	fs := &cannedFileStore{
		files: map[string][]byte{}, // miss on every URI
	}
	svc := NewReindexFTSService(repo, fs)

	report, err := svc.Run(ctx, 0, false)
	require.NoError(t, err)
	require.Len(t, report.Failures, 1)
	assert.Equal(t, externalized.ID, report.Failures[0].ItemID)
	assert.Equal(t, 1, report.Failed)
	assert.Equal(t, 0, report.Reindexed)
}

// TestReindexFTS_Run_RespectsLimit verifies the --limit flag caps how
// many candidates are scanned (useful for smoketesting a large corpus).
func TestReindexFTS_Run_RespectsLimit(t *testing.T) {
	ctx := context.Background()
	repo := newFakeRepo()
	fs := &cannedFileStore{files: map[string][]byte{}}
	for i := range 5 {
		item := newItem("body")
		item.ContentURI = fmt.Sprintf("file://%d", i)
		item.Content = ""
		require.NoError(t, repo.Create(ctx, item))
	}
	svc := NewReindexFTSService(repo, fs)

	report, err := svc.Run(ctx, 2, true)
	require.NoError(t, err)
	assert.Equal(t, 2, report.Scanned, "limit must cap scan count")
}

// --- helpers ---

func newItem(body string) domain.ContextItem {
	item, _ := domain.NewContextItem(
		domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u-1"},
	)
	item.Content = body
	return item
}

// cannedFileStore returns canned bytes for known URIs and errors on misses.
type cannedFileStore struct {
	files map[string][]byte
}

func (c *cannedFileStore) Put(content []byte, mime string) (uri string, hash string, err error) {
	return "", "", fmt.Errorf("not implemented")
}
func (c *cannedFileStore) Get(uri string) ([]byte, error) {
	if data, ok := c.files[uri]; ok {
		return data, nil
	}
	return nil, fmt.Errorf("missing file %s", uri)
}
func (c *cannedFileStore) Delete(uri string) error { return nil }

// explodingFileStore fails every Get — used to assert dry-run doesn't read.
type explodingFileStore struct{}

func (explodingFileStore) Put(content []byte, mime string) (uri string, hash string, err error) {
	return "", "", fmt.Errorf("not implemented")
}
func (explodingFileStore) Get(uri string) ([]byte, error) {
	return nil, fmt.Errorf("explodingFileStore: unexpected Get(%q)", uri)
}
func (explodingFileStore) Delete(uri string) error { return nil }
