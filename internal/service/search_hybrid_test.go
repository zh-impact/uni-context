package service

import (
	"context"
	"sort"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/adapter/embedder/fake"
	"uni-context/internal/adapter/sqlite"
	"uni-context/internal/domain"
	"uni-context/internal/port"
)

// newHybridFixture wires a real sqlite repo + searcher + fake embedder +
// vector store against one in-memory DB (so the searcher's VectorStore
// JOINs see the same context_item rows the repo writes). Mirrors
// newMemVectorStore's pattern but also returns a SearchService with the
// embedder attached.
func newHybridFixture(t *testing.T) (*SearchService, port.VectorStore, *fake.Embedder, port.ContextRepo) {
	t.Helper()
	vs, repo, db := newMemVectorStore(t)
	t.Cleanup(func() { _ = db.Close() })
	emb := fake.New("fake-model", 8)
	// sqlite.NewSearcher wires its own VectorStore against the same *sql.DB,
	// so it sees every vector that the returned `vs` writes. No need to
	// share the VectorStore instance.
	searcher := sqlite.NewSearcher(db)
	svc := NewSearchServiceWithEmbedder(searcher, repo, emb)
	return svc, vs, emb, repo
}

// embedAndPut is the test's shortcut for what EmbedService.Embed does:
// embed the composed text under the fake model and store it. The flag
// flip is irrelevant for hybrid search (which doesn't read any_embedding),
// so we skip it to keep the test focused.
func embedAndPut(t *testing.T, ctx context.Context, emb *fake.Embedder, vs port.VectorStore, itemID, text string) {
	t.Helper()
	vecs, err := emb.Embed(ctx, []string{text})
	require.NoError(t, err)
	require.NoError(t, vs.Put(ctx, "fake-model", itemID, vecs[0]))
}

func TestSearchService_Hybrid_FusesFTSAndVector(t *testing.T) {
	svc, vs, emb, repo := newHybridFixture(t)
	ctx := context.Background()

	// Item A: strong FTS match. Title contains the query term "deploy".
	a, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	a.Title = "deploy guide"
	a.Content = "how to deploy go to k8s"
	require.NoError(t, repo.Create(ctx, a))
	embedAndPut(t, ctx, emb, vs, a.ID, a.Title+"\n\n"+a.Content)

	// Item B: strong vector match. To make the fake embedding for B close
	// to the embedding for the query "deploy", we feed B's embed text equal
	// to the query — the fake embedder is deterministic, so identical texts
	// produce identical vectors, guaranteeing distance 0 (the strongest
	// possible match). B's title and content do NOT contain "deploy" so
	// FTS will not match it.
	b, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	b.Title = "shipping runbook"
	b.Content = "rolling update procedure"
	require.NoError(t, repo.Create(ctx, b))
	// Embed text chosen to equal the hybrid query below.
	embedAndPut(t, ctx, emb, vs, b.ID, "deploy")

	// Item C: neither FTS nor vector relevant. Its embedding is for an
	// unrelated text so its vector distance to the query is large.
	c, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	c.Title = "cooking recipes"
	c.Content = "pasta carbonara ingredients"
	require.NoError(t, repo.Create(ctx, c))
	embedAndPut(t, ctx, emb, vs, c.ID, c.Title+"\n\n"+c.Content)

	resp, err := svc.Search(ctx, SearchRequest{
		Query: "deploy", Mode: SearchModeHybrid, Limit: 5,
	})
	require.NoError(t, err)
	require.NotEmpty(t, resp.Results, "hybrid search must return results")

	// Both A and B must be in the results; C must be absent or rank last.
	ids := make(map[string]int, len(resp.Results))
	for i, r := range resp.Results {
		ids[r.Item.ID] = i
	}
	_, aIn := ids[a.ID]
	_, bIn := ids[b.ID]
	assert.True(t, aIn, "A (FTS match) must be in hybrid results")
	assert.True(t, bIn, "B (vector match) must be in hybrid results")

	if cIdx, ok := ids[c.ID]; ok {
		// C may legitimately not appear at all (it's neither). If it does
		// appear it must rank below both A and B.
		assert.Greater(t, cIdx, ids[a.ID], "C must rank below A")
		assert.Greater(t, cIdx, ids[b.ID], "C must rank below B")
	}

	// A must be marked as matched by FTS; B must be marked as matched by
	// vector. (B's title/content do not contain "deploy", so FTS won't
	// surface it.)
	assert.Contains(t, resp.Results[ids[a.ID]].MatchedBy, "fts",
		"A should be flagged as an FTS match")
	assert.Contains(t, resp.Results[ids[b.ID]].MatchedBy, "vector",
		"B should be flagged as a vector match")
}

func TestSearchService_Hybrid_DedupesItemsHitByBoth(t *testing.T) {
	// If item A is returned by both FTS and vector search, RRF must
	// produce one entry for it (not two), and that entry's matched_by
	// should include both.
	svc, vs, emb, repo := newHybridFixture(t)
	ctx := context.Background()

	// Item A: title contains "deploy" (FTS will match) AND we embed it
	// with text identical to the query "deploy" (vector will match at
	// distance 0, top of KNN). So A appears in both lists.
	a, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	a.Title = "deploy guide"
	a.Content = "deploy deploy deploy"
	require.NoError(t, repo.Create(ctx, a))
	embedAndPut(t, ctx, emb, vs, a.ID, "deploy")

	// Item B: only FTS match (title contains "deploy", embedding for an
	// unrelated text so vector distance is large).
	b, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	b.Title = "deploy runbook"
	b.Content = "deploy steps"
	require.NoError(t, repo.Create(ctx, b))
	embedAndPut(t, ctx, emb, vs, b.ID, b.Title+"\n\n"+b.Content)

	resp, err := svc.Search(ctx, SearchRequest{
		Query: "deploy", Mode: SearchModeHybrid, Limit: 10,
	})
	require.NoError(t, err)

	// A should appear exactly once.
	aCount := 0
	var aResult *SearchResult
	for i := range resp.Results {
		if resp.Results[i].Item.ID == a.ID {
			aCount++
			aResult = &resp.Results[i]
		}
	}
	assert.Equal(t, 1, aCount, "item hit by both FTS and vector must be deduped to one row")

	// A's matched_by must include both "fts" and "vector".
	require.NotNil(t, aResult)
	sort.Strings(aResult.MatchedBy)
	assert.Equal(t, []string{"fts", "vector"}, aResult.MatchedBy,
		"item hit by both must be flagged with both match sources")

	// A's RRF score must be strictly greater than B's: A gets two rank
	// contributions (1/(rank_fts+K) + 1/(rank_vec+K)), B gets only one
	// (1/(rank_fts+K)). Even if A ranks lower than B on FTS alone, the
	// additional vector contribution pushes A's total above B's.
	var bScore float64
	for _, r := range resp.Results {
		if r.Item.ID == b.ID {
			bScore = r.Score
		}
	}
	assert.Greater(t, aResult.Score, bScore,
		"item matched by both FTS and vector (A) must outrank item matched by FTS only (B)")
}

func TestSearchService_Hybrid_DegradesToFTSWhenEmbedderNil(t *testing.T) {
	// If no embedder is wired (NewSearchService, not ...WithEmbedder),
	// a hybrid request must silently downgrade to fts-only — no error,
	// results still come back, and MatchedBy is fts-only.
	_, repo, db := newMemVectorStore(t)
	t.Cleanup(func() { _ = db.Close() })
	searcher := sqlite.NewSearcher(db)
	svc := NewSearchService(searcher, repo) // no embedder

	ctx := context.Background()
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "deploy"
	item.Content = "deploy steps"
	require.NoError(t, repo.Create(ctx, item))

	resp, err := svc.Search(ctx, SearchRequest{
		Query: "deploy", Mode: SearchModeHybrid, Limit: 5,
	})
	require.NoError(t, err, "hybrid with nil embedder must not error")
	require.Len(t, resp.Results, 1)
	assert.Equal(t, []string{"fts"}, resp.Results[0].MatchedBy,
		"degraded hybrid result must be flagged fts-only")
}
