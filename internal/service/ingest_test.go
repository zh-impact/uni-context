package service

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"uni-context/internal/domain"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestIngest_Create_SmallContentInline(t *testing.T) {
	f := newIngestFixture(t)
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Title:       "Test",
		Content:     "small content",
		Tags:        []string{"t1"},
	})
	require.NoError(t, err)
	assert.NotEmpty(t, id)

	got, err := f.repo.Get(context.Background(), id)
	require.NoError(t, err)
	assert.Equal(t, "Test", got.Title)
	assert.Equal(t, "small content", got.Content)
	assert.Empty(t, got.ContentURI)
	assert.Equal(t, []string{"t1"}, got.Tags)
	assert.Greater(t, got.WordCount, 0)
}

func TestIngest_Create_LargeContentExternalized(t *testing.T) {
	f := newIngestFixture(t)
	large := strings.Repeat("word ", 1000) // ~5KB
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     large,
	})
	require.NoError(t, err)

	got, _ := f.repo.Get(context.Background(), id)
	assert.Empty(t, got.Content, "inline content should be emptied")
	assert.NotEmpty(t, got.ContentURI, "content_uri should be set")
	assert.Contains(t, got.ContentURI, "file://")
	assert.NotEmpty(t, got.ContentHash)

	// FileStore can resolve the content
	data, err := f.fs.Get(got.ContentURI)
	require.NoError(t, err)
	assert.Equal(t, large, string(data))
}

func TestIngest_Create_RejectsInvalidScope(t *testing.T) {
	f := newIngestFixture(t)
	_, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeGlobal, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1", // invalid with global
	})
	require.Error(t, err)
	assert.ErrorIs(t, err, domain.ErrValidation)
}

func TestIngest_Create_DeduplicatesByContentHash(t *testing.T) {
	f := newIngestFixture(t)
	content := strings.Repeat("a", 5000)
	id1, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     content,
	})
	require.NoError(t, err)

	id2, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     content,
	})
	require.NoError(t, err)

	// Two items, same hash, single filestore entry
	assert.NotEqual(t, id1, id2)
	got1, _ := f.repo.Get(context.Background(), id1)
	got2, _ := f.repo.Get(context.Background(), id2)
	assert.Equal(t, got1.ContentHash, got2.ContentHash)
	assert.Equal(t, got1.ContentURI, got2.ContentURI)
}

// TestIngest_Create_RollsBackFileStoreOnRepoFailure locks in I2: when
// large content has been externalized via fs.Put but repo.Create then
// fails, the service must call fs.Delete to drop the refcount back to 0
// (removing the file). Otherwise the filestore accumulates orphaned
// refcount=1 entries that nothing references — a leak that becomes a
// correctness problem in Plan 2 where the same flow also writes
// embeddings.
func TestIngest_Create_RollsBackFileStoreOnRepoFailure(t *testing.T) {
	f := newIngestFixture(t)
	large := strings.Repeat("a", 5000) // exceeds ContentInlineLimit (4KB)

	// Force repo.Create to fail on the next call.
	f.repo.createErr = fmt.Errorf("simulated persistence failure")

	_, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     large,
	})
	require.Error(t, err, "Create should propagate the repo error")

	// fsstore layout: <root>/<hex[:2]>/<hex> + <hex>.meta. After Put +
	// Delete (refcount 1→0), both files are removed. The fixture's fsRoot
	// starts empty (t.TempDir), so any leftover file = orphan = rollback
	// failed.
	var orphans []string
	err = filepath.WalkDir(f.fsRoot, func(path string, d os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if d.IsDir() {
			return nil
		}
		// fsRoot itself is empty dir; bucket dirs are fine if empty.
		orphans = append(orphans, path)
		return nil
	})
	require.NoError(t, err)
	assert.Empty(t, orphans,
		"filestore should be empty after rollback; found orphaned files: %v", orphans)
}
