package sqlite

import (
	"context"
	"database/sql"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"uni-context/internal/domain"
	"uni-context/internal/port"
)

func setupRepo(t *testing.T) (port.ContextRepo, *sql.DB) {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	require.NoError(t, Migrate(db))
	t.Cleanup(func() { db.Close() })
	return NewContextRepo(db), db
}

func newItem(t *testing.T, scope domain.Scope, kind domain.Kind, source domain.Source) domain.ContextItem {
	t.Helper()
	params := domain.NewItemParams{OwnerUserID: "u-1"}
	if scope == domain.ScopeProject {
		params = domain.NewItemParams{OwnerUserID: "u-1", ProjectID: "p-1"}
	}
	if scope == domain.ScopeGlobal {
		params = domain.NewItemParams{}
	}
	item, err := domain.NewContextItem(scope, kind, source, params)
	require.NoError(t, err)
	item.Title = "Test Note"
	item.Content = "Hello world from a test note."
	return item
}

func TestContextRepo_CreateAndGet(t *testing.T) {
	repo, _ := setupRepo(t)
	ctx := context.Background()
	item := newItem(t, domain.ScopeUser, domain.KindNote, domain.SourceManual)

	require.NoError(t, repo.Create(ctx, item))

	got, err := repo.Get(ctx, item.ID)
	require.NoError(t, err)
	assert.Equal(t, item.ID, got.ID)
	assert.Equal(t, "Test Note", got.Title)
	assert.Equal(t, "Hello world from a test note.", got.Content)
	assert.Equal(t, []string{}, got.Tags)
}

func TestContextRepo_GetNotFound(t *testing.T) {
	repo, _ := setupRepo(t)
	_, err := repo.Get(context.Background(), "nonexistent")
	assert.ErrorIs(t, err, domain.ErrNotFound)
}

func TestContextRepo_Delete(t *testing.T) {
	repo, _ := setupRepo(t)
	ctx := context.Background()
	item := newItem(t, domain.ScopeUser, domain.KindNote, domain.SourceManual)
	require.NoError(t, repo.Create(ctx, item))

	require.NoError(t, repo.Delete(ctx, item.ID))

	_, err := repo.Get(ctx, item.ID)
	assert.ErrorIs(t, err, domain.ErrNotFound)
}

func TestContextRepo_Update(t *testing.T) {
	repo, _ := setupRepo(t)
	ctx := context.Background()
	item := newItem(t, domain.ScopeUser, domain.KindNote, domain.SourceManual)
	require.NoError(t, repo.Create(ctx, item))

	item.Title = "Updated"
	item.Content = "New content"
	require.NoError(t, repo.Update(ctx, item))

	got, err := repo.Get(ctx, item.ID)
	require.NoError(t, err)
	assert.Equal(t, "Updated", got.Title)
	assert.Equal(t, "New content", got.Content)
}

func TestContextRepo_ListWithFilter(t *testing.T) {
	repo, _ := setupRepo(t)
	ctx := context.Background()

	for _, k := range []domain.Kind{domain.KindNote, domain.KindNote, domain.KindLink} {
		item := newItem(t, domain.ScopeUser, k, domain.SourceManual)
		if k == domain.KindLink {
			item.Title = "Link"
		}
		require.NoError(t, repo.Create(ctx, item))
	}

	items, _, err := repo.List(ctx, port.ItemFilter{
		Scopes: []domain.Scope{domain.ScopeUser},
		Kinds:  []domain.Kind{domain.KindNote},
		Limit:  10,
	})
	require.NoError(t, err)
	assert.Len(t, items, 2)
}

func TestContextRepo_CursorPagination(t *testing.T) {
	repo, _ := setupRepo(t)
	ctx := context.Background()
	for i := 0; i < 25; i++ {
		item := newItem(t, domain.ScopeUser, domain.KindNote, domain.SourceManual)
		require.NoError(t, repo.Create(ctx, item))
	}

	page1, cursor, err := repo.List(ctx, port.ItemFilter{
		Scopes: []domain.Scope{domain.ScopeUser}, Limit: 10,
	})
	require.NoError(t, err)
	assert.Len(t, page1, 10)
	assert.NotEmpty(t, cursor)

	page2, _, err := repo.List(ctx, port.ItemFilter{
		Scopes: []domain.Scope{domain.ScopeUser}, Limit: 10, Cursor: cursor,
	})
	require.NoError(t, err)
	assert.Len(t, page2, 10)

	// No overlap
	seen := map[string]bool{}
	for _, it := range append(append([]domain.ContextItem{}, page1...), page2...) {
		require.False(t, seen[it.ID], "duplicate id across pages")
		seen[it.ID] = true
	}
}
