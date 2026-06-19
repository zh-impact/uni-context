package sqlite

import (
	"context"
	"database/sql"
	"testing"

	"uni-context/internal/domain"
	"uni-context/internal/port"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
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

func TestContextRepo_ListWithTagsFilter(t *testing.T) {
	// Tags filter uses OR semantics: an item matches if it has ANY of the
	// requested tags. This matches the CLI UX intuition
	// (`--tag go --tag python` = "go OR python"). Tags are stored as a JSON
	// array, so the SQL uses json_each to enumerate the item's tags.
	repo, _ := setupRepo(t)
	ctx := context.Background()

	withTags := func(tags ...string) domain.ContextItem {
		item := newItem(t, domain.ScopeUser, domain.KindNote, domain.SourceManual)
		item.Tags = tags
		return item
	}
	require.NoError(t, repo.Create(ctx, withTags("go", "deploy")))
	require.NoError(t, repo.Create(ctx, withTags("python", "scrape")))
	require.NoError(t, repo.Create(ctx, withTags("go", "test")))
	require.NoError(t, repo.Create(ctx, withTags("rust"))) // no overlap

	// Single tag → 2 items (go appears on items 1 and 3)
	items, _, err := repo.List(ctx, port.ItemFilter{
		Tags:  []string{"go"},
		Limit: 50,
	})
	require.NoError(t, err)
	assert.Len(t, items, 2, "single tag 'go' should match 2 items")

	// Two tags OR → 3 items (go×2 + python×1)
	items, _, err = repo.List(ctx, port.ItemFilter{
		Tags:  []string{"go", "python"},
		Limit: 50,
	})
	require.NoError(t, err)
	assert.Len(t, items, 3, "OR of {go, python} should match 3 items")

	// Empty Tags → no filter, returns all 4
	items, _, err = repo.List(ctx, port.ItemFilter{Limit: 50})
	require.NoError(t, err)
	assert.Len(t, items, 4, "empty Tags should not filter")

	// Non-matching tag → 0 items
	items, _, err = repo.List(ctx, port.ItemFilter{
		Tags:  []string{"java"},
		Limit: 50,
	})
	require.NoError(t, err)
	assert.Empty(t, items)
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
