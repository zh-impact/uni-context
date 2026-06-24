package service

import (
	"context"
	"fmt"
	"sort"
	"testing"
	"time"

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

// errorEmbedder wraps a real Embedder but forces Embed to always fail.
// Used to prove the embed-failure path in searchHybrid degrades to
// fts-only instead of bubbling the error up.
type errorEmbedder struct {
	inner *fake.Embedder
}

func (e *errorEmbedder) Model() port.ModelInfo { return e.inner.Model() }
func (e *errorEmbedder) Embed(_ context.Context, _ []string) ([][]float32, error) {
	return nil, fmt.Errorf("simulated ollama outage")
}

// TestSearchService_Hybrid_DegradesToFTSOnEmbedError proves the Plan 2a
// global constraint: when an embedder exists but errors transiently
// (Ollama down, network blip, vector store corruption), searchHybrid
// must NOT abort the whole search — it warns and falls back to fts-only,
// returning whatever FTS finds with MatchedBy=["fts"].
func TestSearchService_Hybrid_DegradesToFTSOnEmbedError(t *testing.T) {
	// Build the same in-memory DB + searcher + repo shape as the
	// nil-embedder test, but wire an errorEmbedder so searchHybrid
	// actually enters the embed-failure branch (rather than being
	// short-circuited by the s.embedder == nil check in Search).
	_, repo, db := newMemVectorStore(t)
	t.Cleanup(func() { _ = db.Close() })
	searcher := sqlite.NewSearcher(db)
	emb := fake.New("fake-model", 8)
	svc := NewSearchServiceWithEmbedder(searcher, repo, &errorEmbedder{inner: emb})

	ctx := context.Background()
	// Seed one FTS-matchable item. Vector writes are intentionally
	// skipped — the test asserts the fts-only fallback path, which must
	// not depend on any vector data being present.
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "deploy"
	item.Content = "deploy steps"
	require.NoError(t, repo.Create(ctx, item))

	resp, err := svc.Search(ctx, SearchRequest{
		Query: "deploy", Mode: SearchModeHybrid, Limit: 5,
	})
	require.NoError(t, err, "hybrid with failing embedder must not error — it degrades to fts-only")
	require.NotEmpty(t, resp.Results, "degraded hybrid must still return FTS hits")
	for _, r := range resp.Results {
		assert.Equal(t, []string{"fts"}, r.MatchedBy,
			"every result from a degraded hybrid search must be flagged fts-only")
	}
}

// TestSearchService_Hybrid_FTSFailureReturnsVectorOnly is the regression
// guard for the asymmetric error-handling bug. The vector-failure path
// was graceful (warn + fall back to fts-only), but the FTS-failure path
// returned the error verbatim and discarded the already-fetched vector
// hits — wasting the embed + KNN work. The fix: when FTS fails, warn
// and proceed with vector-only fusion, mirroring the vector-failure path.
//
// Setup: stub Searcher whose SearchVector returns one canned hit and
// whose SearchFTS returns an error. The response must:
//   - not propagate the FTS error
//   - contain the vector hit
//   - flag the hit as MatchedBy=["vector"] only
func TestSearchService_Hybrid_FTSFailureReturnsVectorOnly(t *testing.T) {
	repo := newFakeRepo()
	item := domain.ContextItem{
		ID: "vec-hit-1", Scope: domain.ScopeUser, Kind: domain.KindNote,
		Title: "vector only", Tags: []string{},
	}
	require.NoError(t, repo.Create(context.Background(), item))

	searcher := &stubSearcher{
		vecHits: []port.VectorHit{{ID: "vec-hit-1", Distance: 0.1, Score: 0.95}},
		ftsErr:  fmt.Errorf("simulated FTS index corruption"),
	}
	emb := fake.New("fake-model", 8)
	svc := NewSearchServiceWithEmbedder(searcher, repo, emb)

	resp, err := svc.Search(context.Background(), SearchRequest{
		Query: "anything", Mode: SearchModeHybrid, Limit: 5,
	})
	require.NoError(t, err, "FTS failure must not propagate; vector results should still be returned")
	require.Len(t, resp.Results, 1, "vector hit must survive FTS failure")
	assert.Equal(t, "vec-hit-1", resp.Results[0].Item.ID)
	assert.Equal(t, []string{"vector"}, resp.Results[0].MatchedBy,
		"result must be flagged as vector-only when FTS failed")
}

// TestSearchService_Hybrid_TiebreakPrefersNewer is the regression guard
// for the sort tiebreak direction. The RRF sort used to break score ties
// by `Item.ID < Item.ID` (ULID dictionary order = older item first). For
// a personal-knowledge base the more useful tiebreak is newer-first: when
// two items are equally relevant, the recent one is more likely what the
// user is looking for.
//
// Setup: two real items created via NewContextItem so their ULIDs reflect
// creation time. stubSearcher returns hits so each item lands at fts-rank
// i and vector-rank (1-i), giving both the identical RRF score
// 1/(0+60) + 1/(1+60). Empty scopes/kinds so no filter interferes.
func TestSearchService_Hybrid_TiebreakPrefersNewer(t *testing.T) {
	repo := newFakeRepo()

	older, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	require.NoError(t, repo.Create(context.Background(), older))

	// Sleep past ULID timestamp granularity so newer.ID > older.ID lexically.
	time.Sleep(15 * time.Millisecond)

	newer, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	require.NoError(t, repo.Create(context.Background(), newer))
	require.Greater(t, newer.ID, older.ID,
		"newer ULID must sort after older for this test to be meaningful")

	// Crossed ranks: older=[fts0, vec1], newer=[fts1, vec0].
	// Both score 1/60 + 1/61 → genuine tie → tiebreak decides.
	searcher := &stubSearcher{
		ftsHits: []port.SearchHit{
			{ID: older.ID, Score: 2.0, Snippet: "older"},
			{ID: newer.ID, Score: 1.0, Snippet: "newer"},
		},
		vecHits: []port.VectorHit{
			{ID: newer.ID, Distance: 0.1, Score: 0.95},
			{ID: older.ID, Distance: 0.2, Score: 0.90},
		},
	}
	emb := fake.New("fake-model", 8)
	svc := NewSearchServiceWithEmbedder(searcher, repo, emb)

	resp, err := svc.Search(context.Background(), SearchRequest{
		Query: "x", Mode: SearchModeHybrid, Limit: 5,
	})
	require.NoError(t, err)
	require.Len(t, resp.Results, 2)

	// Sanity: the test setup must produce a real tie, not a near-tie.
	assert.InDelta(t, resp.Results[0].Score, resp.Results[1].Score, 1e-9,
		"fts rank 0 + vec rank 1 must equal fts rank 1 + vec rank 0")

	assert.Equal(t, newer.ID, resp.Results[0].Item.ID,
		"on score tie, newer item (higher ULID) must rank first")
	assert.Equal(t, older.ID, resp.Results[1].Item.ID)
}

// stubSearcher is a port.Searcher with controllable behavior. Used to
// exercise error paths in searchHybrid without a real sqlite backend.
type stubSearcher struct {
	vecHits []port.VectorHit
	vecErr  error
	ftsHits []port.SearchHit
	ftsErr  error
}

func (s *stubSearcher) SearchFTS(_ context.Context, _ port.SearchQuery) ([]port.SearchHit, error) {
	return s.ftsHits, s.ftsErr
}

func (s *stubSearcher) SearchVector(_ context.Context, _ port.VectorQuery) ([]port.VectorHit, error) {
	return s.vecHits, s.vecErr
}

// TestSearchService_Hybrid_FTSRankIsPostFilter is the regression guard
// for the rank-semantics asymmetry. The FTS fusion loop used to use the
// raw `for rank, h := range fHits` index, but fHits comes back
// UNFILTERED — scope/kind filtering happens in Go after hydration. So
// if N items are filtered before item X, X's RRF contribution was
// 1/(N+60) instead of 1/60. The vector loop is post-filter (filters
// push down via JOIN), so vector ranks were already correct.
//
// Setup: stub returns fHits = [g1(global,rank0), g2(global,rank1),
// u1(user,rank2)]. Scopes=[user] drops g1+g2. u1 should score as the
// post-filter rank-0 item (1/60), not the pre-filter rank-2 item
// (1/62). Empty vHits isolates the FTS contribution for the assertion.
func TestSearchService_Hybrid_FTSRankIsPostFilter(t *testing.T) {
	repo := newFakeRepo()
	for _, it := range []domain.ContextItem{
		{ID: "g1", Scope: domain.ScopeGlobal, Kind: domain.KindDoc, Tags: []string{}},
		{ID: "g2", Scope: domain.ScopeGlobal, Kind: domain.KindDoc, Tags: []string{}},
		{ID: "u1", Scope: domain.ScopeUser, Kind: domain.KindNote, Tags: []string{}},
	} {
		require.NoError(t, repo.Create(context.Background(), it))
	}

	searcher := &stubSearcher{
		ftsHits: []port.SearchHit{
			{ID: "g1", Score: 3.0},
			{ID: "g2", Score: 2.0},
			{ID: "u1", Score: 1.0},
		},
		// vecHits empty: isolates FTS contribution in u1's final score.
	}
	emb := fake.New("fake-model", 8)
	svc := NewSearchServiceWithEmbedder(searcher, repo, emb)

	resp, err := svc.Search(context.Background(), SearchRequest{
		Query:  "x",
		Scopes: []domain.Scope{domain.ScopeUser},
		Mode:   SearchModeHybrid,
		Limit:  5,
	})
	require.NoError(t, err)
	require.Len(t, resp.Results, 1, "only u1 should survive scope filter")
	assert.Equal(t, "u1", resp.Results[0].Item.ID)

	// Post-filter rank 0 → contribution 1/(0+60) = 1/60.
	// Pre-filter rank 2 (the bug) → 1/(2+60) = 1/62.
	// Difference is ~0.00054, well outside float64 noise; 1e-9 tolerance.
	wantScore := 1.0 / float64(0+rrfK)
	assert.InDelta(t, wantScore, resp.Results[0].Score, 1e-9,
		"u1 must score as post-filter rank 0 (1/60=%.6f); got %.6f (pre-filter rank 2 = 1/62=%.6f)",
		wantScore, resp.Results[0].Score, 1.0/62.0)
}
