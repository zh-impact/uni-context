package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

// TestItemService_Get_HydratesInlineContent: when an item carries inline
// Content, Get returns it verbatim without touching FileStore. Proves the
// inline fast path and that FileStore is not a hard dependency for inline items.
func TestItemService_Get_HydratesInlineContent(t *testing.T) {
	repo := newFakeRepo()
	// explodingFileStore: any Get call fails the test — proves inline
	// items never hit the externalized path.
	svc := NewItemService(repo, &explodingFileStore{})

	item := newItem("inline body")
	require.NoError(t, repo.Create(context.Background(), item))

	got, err := svc.Get(context.Background(), item.ID)
	require.NoError(t, err)
	assert.Equal(t, "inline body", got.Content,
		"inline content returned verbatim, no FS read")
}

// TestItemService_Get_HydratesExternalizedContent: when an item has empty
// Content + a ContentURI (the post-Create state for >4KB items), Get loads
// the body from FileStore and fills Content. This is the policy the CLI's
// get command used to rediscover inline (user_note.go:184-191).
func TestItemService_Get_HydratesExternalizedContent(t *testing.T) {
	repo := newFakeRepo()
	fs := &cannedFileStore{
		files: map[string][]byte{
			"file://deadbeef": []byte("externalized body"),
		},
	}
	svc := NewItemService(repo, fs)

	item := newItem("")
	item.ContentURI = "file://deadbeef"
	require.NoError(t, repo.Create(context.Background(), item))

	got, err := svc.Get(context.Background(), item.ID)
	require.NoError(t, err)
	assert.Equal(t, "externalized body", got.Content,
		"externalized content loaded from FileStore into Content")
}

// TestItemService_Get_MissingItemReturnsNotFound: repo miss propagates as
// domain.ErrNotFound (wrapped), so callers can distinguish missing-item from
// hydration failures.
func TestItemService_Get_MissingItemReturnsNotFound(t *testing.T) {
	repo := newFakeRepo()
	svc := NewItemService(repo, &explodingFileStore{})

	_, err := svc.Get(context.Background(), "nope")
	require.Error(t, err)
	assert.ErrorIs(t, err, domain.ErrNotFound)
}

// TestItemService_Get_ExternalizedButFSMiss: when ContentURI points at a
// FileStore entry that no longer exists (file deleted, refcount bug), the
// error must surface the URI so the operator can find the dangling pointer.
func TestItemService_Get_ExternalizedButFSMiss(t *testing.T) {
	repo := newFakeRepo()
	// empty map → Get errors for every URI
	svc := NewItemService(repo, &cannedFileStore{files: map[string][]byte{}})

	item := newItem("")
	item.ContentURI = "file://missing"
	require.NoError(t, repo.Create(context.Background(), item))

	_, err := svc.Get(context.Background(), item.ID)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "file://missing",
		"FS miss error must be wrapped and mention the URI")
}

// TestItemService_List_DelegatesToRepo: List is a thin pass-through to
// repo.List with the caller's filter. Asserts filter + cursor pass through
// and all items return.
func TestItemService_List_DelegatesToRepo(t *testing.T) {
	repo := newFakeRepo()
	svc := NewItemService(repo, &explodingFileStore{})

	a := newItem("a")
	b := newItem("b")
	require.NoError(t, repo.Create(context.Background(), a))
	require.NoError(t, repo.Create(context.Background(), b))

	items, cursor, err := svc.List(context.Background(), port.ItemFilter{
		Scopes: []domain.Scope{domain.ScopeUser},
	})
	require.NoError(t, err)
	assert.Len(t, items, 2)
	assert.Empty(t, cursor, "fakeRepo returns empty cursor")
}

// TestItemService_Delete_DelegatesToRepo: Delete removes the item via the
// repo; a subsequent Get must miss.
func TestItemService_Delete_DelegatesToRepo(t *testing.T) {
	repo := newFakeRepo()
	svc := NewItemService(repo, &explodingFileStore{})

	item := newItem("body")
	require.NoError(t, repo.Create(context.Background(), item))

	require.NoError(t, svc.Delete(context.Background(), item.ID))

	_, err := repo.Get(context.Background(), item.ID)
	require.Error(t, err, "item must be gone after Delete")
}
