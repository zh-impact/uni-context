package sqlite

import (
	"context"
	"database/sql"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

func newEmbeddingRepoFixture(t *testing.T) (port.EmbeddingRepo, *ContextRepo, *sql.DB) {
	t.Helper()
	db := openTestDB(t) // from model_registry_test.go — fresh migrated :memory:
	repo := NewContextRepo(db)
	embRepo := NewEmbeddingRepo(db)
	return embRepo, repo, db
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
	embRepo, repo, _ := newEmbeddingRepoFixture(t)
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
	embRepo, repo, _ := newEmbeddingRepoFixture(t)
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
	embRepo, _, _ := newEmbeddingRepoFixture(t)
	_, err := embRepo.GetStatus(context.Background(), "nonexistent", "bge-m3")
	require.Error(t, err)
	assert.ErrorIs(t, err, domain.ErrNotFound)
}

func TestEmbeddingRepo_ListFailed_BasicOrdering(t *testing.T) {
	// Replacement test using sleeps to guarantee ordering (no raw SQL needed).
	embRepo, repo, _ := newEmbeddingRepoFixture(t)
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

// TestEmbeddingRepo_ListForItem covers the four behaviors the CLI command
// depends on: empty slice (not nil) for missing IDs, single row, multi-
// model ordering by model_slug ASC, and columns scanning correctly.
func TestEmbeddingRepo_ListForItem(t *testing.T) {
	t.Run("missing item returns empty slice not nil", func(t *testing.T) {
		embRepo, _, _ := newEmbeddingRepoFixture(t)
		rows, err := embRepo.ListForItem(context.Background(), "no-such-item")
		require.NoError(t, err)
		require.NotNil(t, rows, "empty slice, not nil — caller uses len()")
		assert.Len(t, rows, 0)
	})

	t.Run("single row returns expected columns", func(t *testing.T) {
		embRepo, repo, _ := newEmbeddingRepoFixture(t)
		insertItemForEmbedTest(t, repo, "i1")
		require.NoError(t, embRepo.UpsertStatus(context.Background(),
			"i1", "bge-m3", "done", ""))

		rows, err := embRepo.ListForItem(context.Background(), "i1")
		require.NoError(t, err)
		require.Len(t, rows, 1)
		assert.Equal(t, "i1", rows[0].ItemID)
		assert.Equal(t, "bge-m3", rows[0].ModelSlug)
		assert.Equal(t, "done", rows[0].Status)
	})

	t.Run("multiple models ordered by slug ASC", func(t *testing.T) {
		embRepo, repo, db := newEmbeddingRepoFixture(t)
		insertItemForEmbedTest(t, repo, "i2")

		// The migration 0002 FK requires embedding_model rows for these
		// slugs (UpsertStatus would otherwise fail FK). Direct INSERT
		// keeps the test focused on ListForItem rather than spinning up
		// per-slug vec tables via Registry.Register. Insert parent rows
		// BEFORE child context_embedding rows so FK passes.
		for _, slug := range []string{"zzz-model", "aaa-model", "mmm-model"} {
			_, err := db.Exec(`
				INSERT OR IGNORE INTO embedding_model
				(slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
				VALUES (?, ?, 'ollama', 8, 'vec_unused_8', 0, 'active', '{}', 0)`,
				slug, slug)
			require.NoError(t, err)
		}

		// Insert in non-alphabetical order; assert sorted output.
		require.NoError(t, embRepo.UpsertStatus(context.Background(),
			"i2", "zzz-model", "done", ""))
		require.NoError(t, embRepo.UpsertStatus(context.Background(),
			"i2", "aaa-model", "failed", "boom"))
		require.NoError(t, embRepo.UpsertStatus(context.Background(),
			"i2", "mmm-model", "done", ""))

		rows, err := embRepo.ListForItem(context.Background(), "i2")
		require.NoError(t, err)
		require.Len(t, rows, 3)
		assert.Equal(t, "aaa-model", rows[0].ModelSlug)
		assert.Equal(t, "mmm-model", rows[1].ModelSlug)
		assert.Equal(t, "zzz-model", rows[2].ModelSlug)
		assert.Equal(t, "failed", rows[0].Status)
		assert.Equal(t, "boom", rows[0].LastError)
	})
}
