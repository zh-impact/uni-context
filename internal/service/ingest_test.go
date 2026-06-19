package service

import (
	"context"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"uni-context/internal/domain"
)

func TestIngest_Create_SmallContentInline(t *testing.T) {
	f := newIngestFixture(t)
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Title:      "Test",
		Content:    "small content",
		Tags:       []string{"t1"},
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
