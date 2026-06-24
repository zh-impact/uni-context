package service

import (
	"context"
	"testing"

	"uni-context/internal/domain"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
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

// TestSearchService_FTSOnly_OverFetchesForScopeFilter is the regression
// guard for the underfill bug. searchFTSOnly used to pass Limit: req.Limit
// to SearchFTS, then post-filter by scope/kind. When the top-Limit FTS
// hits are dominated by out-of-scope items, post-filter silently drops
// them and the user sees fewer results than requested — even though more
// in-scope matches exist further down the BM25 ranking.
//
// Setup: seed 10 ScopeGlobal items whose title contains the needle
// (title column is short → high BM25 density → top of ranking), plus 5
// ScopeUser items with the needle in content (longer document → lower
// BM25 score). Search with Limit=5 + Scopes=[user].
//
// Without the fix: FTS returns top 5 (all globals), post-filter drops
// all 5 → 0 results. With 3× over-fetch: FTS returns 15, post-filter
// keeps the 5 user items.
func TestSearchService_FTSOnly_OverFetchesForScopeFilter(t *testing.T) {
	f := newSearchFixture(t)
	ctx := context.Background()

	// Globals: title match dominates BM25 (short column → high density).
	for range 10 {
		_, err := f.ingest.Create(ctx, Input{
			Scope: domain.ScopeGlobal, Kind: domain.KindDoc, Source: domain.SourceImport,
			Title:   "needle",
			Content: "",
		})
		require.NoError(t, err)
	}

	// Users: needle buried in longer content → lower BM25 score.
	wantIDs := make(map[string]bool, 5)
	for range 5 {
		id, err := f.ingest.Create(ctx, Input{
			Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
			OwnerUserID: "u-1",
			Title:       "user note",
			Content:     "needle buried in a much longer content body that dilutes bm25 density",
		})
		require.NoError(t, err)
		wantIDs[id] = true
	}

	resp, err := f.svc.Search(ctx, SearchRequest{
		Query:  "needle",
		Scopes: []domain.Scope{domain.ScopeUser},
		Limit:  5,
	})
	require.NoError(t, err)
	require.Len(t, resp.Results, 5,
		"fts-only must over-fetch so scope post-filter does not silently underfill")

	for _, r := range resp.Results {
		assert.Equal(t, domain.ScopeUser, r.Item.Scope,
			"post-filter must drop out-of-scope items")
		assert.True(t, wantIDs[r.Item.ID],
			"result %s was not one of the seeded user items", r.Item.ID)
	}
}
