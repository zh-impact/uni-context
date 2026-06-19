package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"uni-context/internal/domain"
)

func TestSearchService_HydratesResults(t *testing.T) {
	f := newSearchFixture(t)
	seedID, _ := f.ingest.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1", Title: "Note", Content: "searchable text here",
	})

	resp, err := f.svc.Search(context.Background(), SearchRequest{
		Query: "searchable", Limit: 10,
	})
	require.NoError(t, err)
	require.Len(t, resp.Results, 1)
	assert.Equal(t, seedID, resp.Results[0].Item.ID)
	assert.NotEmpty(t, resp.Results[0].Snippet)
	assert.Greater(t, resp.Results[0].Score, 0.0)
}

func TestSearchService_FiltersByScope(t *testing.T) {
	f := newSearchFixture(t)
	_, _ = f.ingest.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1", Content: "common keyword",
	})
	_, _ = f.ingest.Create(context.Background(), Input{
		Scope: domain.ScopeGlobal, Kind: domain.KindDoc, Source: domain.SourceImport,
		Content: "common keyword",
	})

	resp, err := f.svc.Search(context.Background(), SearchRequest{
		Query: "common", Scopes: []domain.Scope{domain.ScopeUser},
	})
	require.NoError(t, err)
	for _, r := range resp.Results {
		assert.Equal(t, domain.ScopeUser, r.Item.Scope)
	}
}
