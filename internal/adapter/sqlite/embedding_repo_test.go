package sqlite

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

func newEmbeddingRepoFixture(t *testing.T) (port.EmbeddingRepo, *ContextRepo) {
	t.Helper()
	db := openTestDB(t) // from model_registry_test.go — fresh migrated :memory:
	repo := NewContextRepo(db)
	embRepo := NewEmbeddingRepo(db)
	return embRepo, repo
}

// insertItemForEmbedTest creates a context_item so FK on context_embedding passes.
func insertItemForEmbedTest(t *testing.T, repo *ContextRepo, id string) {
	t.Helper()
	item, err := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	require.NoError(t, err)
	item.ID = id
	require.NoError(t, repo.Create(context.Background(), item))
}

func TestEmbeddingRepo_UpsertStatus_InsertsFresh(t *testing.T) {
	embRepo, repo := newEmbeddingRepoFixture(t)
	insertItemForEmbedTest(t, repo, "item-1")

	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"item-1", "bge-m3", "done", ""))

	st, err := embRepo.GetStatus(context.Background(), "item-1", "bge-m3")
	require.NoError(t, err)
	assert.Equal(t, "done", st.Status)
	assert.Equal(t, 1, st.Attempts, "fresh INSERT starts at attempts=1")
	assert.Empty(t, st.LastError)
	assert.WithinDuration(t, time.Now(), st.EmbeddedAt, 5*time.Second)
}

func TestEmbeddingRepo_UpsertStatus_OnConflictIncrementsAttempts(t *testing.T) {
	embRepo, repo := newEmbeddingRepoFixture(t)
	insertItemForEmbedTest(t, repo, "item-2")

	// First attempt fails
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"item-2", "bge-m3", "failed", "ollama unreachable"))
	st, _ := embRepo.GetStatus(context.Background(), "item-2", "bge-m3")
	assert.Equal(t, 1, st.Attempts)
	assert.Equal(t, "ollama unreachable", st.LastError)

	// Second attempt also fails — attempts increments to 2
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"item-2", "bge-m3", "failed", "still unreachable"))
	st, _ = embRepo.GetStatus(context.Background(), "item-2", "bge-m3")
	assert.Equal(t, 2, st.Attempts)
	assert.Equal(t, "still unreachable", st.LastError)

	// Third attempt succeeds — attempts increments to 3, last_error cleared
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"item-2", "bge-m3", "done", ""))
	st, _ = embRepo.GetStatus(context.Background(), "item-2", "bge-m3")
	assert.Equal(t, 3, st.Attempts)
	assert.Equal(t, "done", st.Status)
	assert.Empty(t, st.LastError)
}

func TestEmbeddingRepo_GetStatus_NotFound(t *testing.T) {
	embRepo, _ := newEmbeddingRepoFixture(t)
	_, err := embRepo.GetStatus(context.Background(), "nonexistent", "bge-m3")
	require.Error(t, err)
	assert.ErrorIs(t, err, domain.ErrNotFound)
}

func TestEmbeddingRepo_ListFailed_BasicOrdering(t *testing.T) {
	// Replacement test using sleeps to guarantee ordering (no raw SQL needed).
	embRepo, repo := newEmbeddingRepoFixture(t)
	insertItemForEmbedTest(t, repo, "first")
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"first", "bge-m3", "failed", "err1"))
	time.Sleep(1100 * time.Millisecond) // embedded_at is unix seconds

	insertItemForEmbedTest(t, repo, "second")
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"second", "bge-m3", "failed", "err2"))

	// Insert a 'done' row that should NOT appear in ListFailed
	insertItemForEmbedTest(t, repo, "done-item")
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"done-item", "bge-m3", "done", ""))

	failed, err := embRepo.ListFailed(context.Background(), 100)
	require.NoError(t, err)
	require.Len(t, failed, 2, "only 'failed' rows returned; 'done' excluded")
	assert.Equal(t, "first", failed[0].ItemID, "oldest failure first")
	assert.Equal(t, "second", failed[1].ItemID)

	// Limit honored
	one, err := embRepo.ListFailed(context.Background(), 1)
	require.NoError(t, err)
	require.Len(t, one, 1)
	assert.Equal(t, "first", one[0].ItemID)
}
