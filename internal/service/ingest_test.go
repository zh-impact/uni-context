package service

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"uni-context/internal/adapter/embedder/fake"
	"uni-context/internal/domain"
	"uni-context/internal/port"

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

// TestIngest_Create_TriggersEmbed_WhenConfigured verifies that when an
// EmbedService is wired in via NewIngestServiceWithEmbedder, Create
// synchronously writes a vector and flips any_embedding=1. This is the
// happy path of Plan 2a's synchronous embed path.
func TestIngest_Create_TriggersEmbed_WhenConfigured(t *testing.T) {
	vs, repo, db := newMemVectorStore(t)
	defer db.Close()
	emb := fake.New("fake-model", 8)
	embedSvc := NewEmbedService(emb, vs, repo)
	svc := NewIngestServiceWithEmbedder(repo, newMemFileStore(t), embedSvc)

	ctx := context.Background()
	id, err := svc.Create(ctx, Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Title:       "deploy",
		Content:     "small",
	})
	require.NoError(t, err)

	// any_embedding flipped to 1 by the embed path
	got, _ := repo.Get(ctx, id)
	assert.Equal(t, 1, got.AnyEmbedding, "Create with embedder should set any_embedding=1")

	// Vector is searchable: query with the fake's embedding of the
	// same composed text the service fed in (title + "\n\n" + content).
	vecs, _ := emb.Embed(ctx, []string{"deploy\n\nsmall"})
	hits, err := vs.Search(ctx, port.VectorQuery{
		Vector: vecs[0], Model: "fake-model", Limit: 5,
	})
	require.NoError(t, err)
	require.Len(t, hits, 1)
	assert.Equal(t, id, hits[0].ID)
}

// TestIngest_Create_SucceedsWhenEmbedFails locks in the error-tolerance
// contract: a broken embedder must NOT fail Create. The item is still
// persisted and FTS-searchable; any_embedding stays 0.
func TestIngest_Create_SucceedsWhenEmbedFails(t *testing.T) {
	vs, repo, db := newMemVectorStore(t)
	defer db.Close()
	emb := &failingEmbedder{}
	embedSvc := NewEmbedService(emb, vs, repo)
	svc := NewIngestServiceWithEmbedder(repo, newMemFileStore(t), embedSvc)

	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     "x",
	})
	require.NoError(t, err, "Create must succeed even if embed fails")
	require.NotEmpty(t, id)

	got, _ := repo.Get(context.Background(), id)
	assert.Equal(t, 0, got.AnyEmbedding, "any_embedding stays 0 on embed failure")
}

// failingEmbedder is a port.Embedder that always errors. Used to verify
// IngestService.Create tolerates embed failures.
type failingEmbedder struct{}

func (failingEmbedder) Model() port.ModelInfo { return port.ModelInfo{Slug: "fail", Dimension: 1} }
func (failingEmbedder) Embed(context.Context, []string) ([][]float32, error) {
	return nil, fmt.Errorf("simulated embedder failure")
}
