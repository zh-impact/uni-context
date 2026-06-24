package service

import (
	"context"
	"fmt"
	"os"
	"sort"
	"time"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

type SearchService struct {
	searcher port.Searcher
	repo     port.ContextRepo
	// embedder is required for hybrid mode. When nil, a hybrid request
	// silently degrades to fts-only (Plan 1 callers are unaffected).
	embedder port.Embedder
	// legTimeout bounds each retrieval leg (vector KNN, FTS) in
	// searchHybrid. Zero (the default for both constructors) means
	// legTimeoutOrDefault applies. A hung leg (vec0 corruption, FTS5
	// tokenizer spin) is cancelled and the existing per-leg fallback
	// path fires instead of blocking the whole search.
	legTimeout time.Duration
}

// legTimeoutOrDefault returns the configured per-leg timeout, defaulting
// to 5s when legTimeout is zero. 5s is generous for local sqlite lookups
// (typical KNN + FTS is <100ms) but bounded enough that a wedged leg
// doesn't make the CLI feel frozen.
func (s *SearchService) legTimeoutOrDefault() time.Duration {
	if s.legTimeout > 0 {
		return s.legTimeout
	}
	return 5 * time.Second
}

// NewSearchService wires the fts-only SearchService (Plan 1 shape). Mode
// defaults to fts-only per request; hybrid requests need
// NewSearchServiceWithEmbedder.
func NewSearchService(searcher port.Searcher, repo port.ContextRepo) *SearchService {
	return &SearchService{searcher: searcher, repo: repo}
}

// NewSearchServiceWithEmbedder wires an embedder for hybrid search. If
// embedder is nil, behavior is identical to NewSearchService. Mirrors
// the NewIngestServiceWithEmbedder pattern from Task 6.
func NewSearchServiceWithEmbedder(searcher port.Searcher, repo port.ContextRepo, embedder port.Embedder) *SearchService {
	return &SearchService{searcher: searcher, repo: repo, embedder: embedder}
}

// SearchMode picks the retrieval strategy for a SearchRequest. Empty
// defaults to fts-only (Plan 1 behavior, backward compatible).
type SearchMode string

const (
	SearchModeFTSOnly SearchMode = "fts-only"
	// SearchModeHybrid runs both FTS and vector KNN with over-fetch = 3×
	// limit, then fuses via Reciprocal Rank Fusion (k=60). Items hit by
	// both contribute two rank terms. Falls back to fts-only when no
	// embedder is wired (svc.embedder == nil).
	SearchModeHybrid SearchMode = "hybrid"
)

type SearchRequest struct {
	Query  string
	Scopes []domain.Scope
	Kinds  []domain.Kind
	Limit  int
	Mode   SearchMode
}

// SearchResult is one row in the response. MatchedBy records which
// retrieval paths contributed to the score: ["fts"], ["vector"], or
// ["fts","vector"] for items hit by both. Order is the order paths
// were folded in (FTS first), then deduped.
type SearchResult struct {
	Item      domain.ContextItem
	Score     float64
	Snippet   string
	MatchedBy []string
}

type SearchResponse struct {
	Results []SearchResult
	Total   int
}

// rrfK is the Reciprocal Rank Fusion constant. 60 is the value used in
// the original RRF paper (Cormack et al. 2009); smaller = top ranks
// dominate more. score(d) = Σ 1/(rank_i + rrfK).
const rrfK = 60

func (s *SearchService) Search(ctx context.Context, req SearchRequest) (SearchResponse, error) {
	mode := req.Mode
	if mode == "" {
		mode = SearchModeFTSOnly
	}
	if mode == SearchModeHybrid && s.embedder == nil {
		// Hybrid requested but no embedder wired — degrade to fts-only.
		// Plan 1 callers that never set Mode are also here.
		mode = SearchModeFTSOnly
	}
	if mode == SearchModeFTSOnly {
		return s.searchFTSOnly(ctx, req)
	}
	return s.searchHybrid(ctx, req)
}

// searchFTSOnly is the Plan 1 retrieval path: FTS + repo hydrate + scope
// /kind post-filter. Each result carries MatchedBy=["fts"].
//
// Over-fetches 3×limit (matching searchHybrid's FTS leg) so post-filter
// trimming by scope/kind doesn't silently underfill the result set.
// Without this, a query whose top-Limit BM25 hits are dominated by
// out-of-scope items would return fewer than Limit results even when
// more in-scope matches exist further down the ranking. Spec §5.2.
func (s *SearchService) searchFTSOnly(ctx context.Context, req SearchRequest) (SearchResponse, error) {
	limit := req.Limit
	if limit <= 0 {
		limit = 20
	}
	overFetch := limit * 3

	hits, err := s.searcher.SearchFTS(ctx, port.SearchQuery{Query: req.Query, Limit: overFetch})
	if err != nil {
		return SearchResponse{}, fmt.Errorf("fts: %w", err)
	}

	scopes := scopeSet(req.Scopes)
	kinds := kindSet(req.Kinds)

	var out []SearchResult
	for _, h := range hits {
		if len(out) >= limit {
			break
		}
		item, err := s.repo.Get(ctx, h.ID)
		if err != nil {
			// item was deleted between FTS row and now; skip
			continue
		}
		if scopes != nil && !scopes[item.Scope] {
			continue
		}
		if kinds != nil && !kinds[item.Kind] {
			continue
		}
		out = append(out, SearchResult{
			Item: item, Score: h.Score, Snippet: h.Snippet, MatchedBy: []string{"fts"},
		})
	}

	return SearchResponse{Results: out, Total: len(out)}, nil
}

// searchHybrid runs vector KNN and FTS sequentially, then
// fuses via RRF. Over-fetches 3×limit from each path so post-filter
// trimming (by scope/kind on the FTS leg, and the RRF fusion trim on
// both legs) still yields req.Limit results. The fused map is trimmed
// to req.Limit at the end. VectorStore honors Limit verbatim — the
// over-fetch lives here at the orchestration layer, per spec §5.2.
func (s *SearchService) searchHybrid(ctx context.Context, req SearchRequest) (SearchResponse, error) {
	limit := req.Limit
	if limit <= 0 {
		limit = 20
	}
	overFetch := limit * 3

	// Plan 2a contract: embed errors during hybrid search must degrade
	// gracefully to fts-only (log + fall back), never abort the whole
	// search. Same applies if the embedder misbehaves (wrong vector count)
	// or the vector store fails transiently (table missing, DB corruption,
	// Ollama down). Mirrors the warn-and-continue pattern in ingest.go:103-105.
	queryVec, err := s.embedder.Embed(ctx, []string{req.Query})
	if err != nil {
		fmt.Fprintf(os.Stderr, "warn: hybrid search embed failed, falling back to fts-only: %v\n", err)
		return s.searchFTSOnly(ctx, req)
	}
	if len(queryVec) != 1 {
		fmt.Fprintf(os.Stderr, "warn: hybrid search embedder returned %d vectors for one query, falling back to fts-only\n", len(queryVec))
		return s.searchFTSOnly(ctx, req)
	}

	scopes := toStrings(req.Scopes)
	kinds := toStrings(req.Kinds)

	// Per-leg timeout so a wedged vector path (vec0 corruption, sqlite-vec
	// bug, lock contention) can't block FTS — and vice versa. The sub-ctx
	// is cancelled as soon as the leg returns; fallbacks use the parent
	// ctx, which is still alive. If the parent ctx already has a tighter
	// deadline, context.WithTimeout honors it (sub-ctx picks the earlier).
	vecCtx, cancelVec := context.WithTimeout(ctx, s.legTimeoutOrDefault())
	vHits, err := s.searcher.SearchVector(vecCtx, port.VectorQuery{
		Vector: queryVec[0], Model: s.embedder.Model().Slug,
		Limit: overFetch, Scopes: scopes, Kinds: kinds,
	})
	cancelVec()
	if err != nil {
		fmt.Fprintf(os.Stderr, "warn: hybrid search vector lookup failed, falling back to fts-only: %v\n", err)
		return s.searchFTSOnly(ctx, req)
	}

	ftsCtx, cancelFTS := context.WithTimeout(ctx, s.legTimeoutOrDefault())
	fHits, err := s.searcher.SearchFTS(ftsCtx, port.SearchQuery{Query: req.Query, Limit: overFetch})
	cancelFTS()
	if err != nil {
		// Symmetric with the vector-failure path above: warn + proceed
		// with what we have. Discarding vHits would waste the embed +
		// KNN work already done. Setting fHits to nil makes the FTS
		// fusion loop a no-op; the vector loop still scores its hits,
		// and the final trim to `limit` returns them as MatchedBy=
		// ["vector"]-only.
		fmt.Fprintf(os.Stderr, "warn: hybrid search fts failed, continuing with vector-only results: %v\n", err)
		fHits = nil
	}

	// RRF: score = Σ 1/(rank + K). Items in both lists get two contributions.
	type fusion struct {
		item      domain.ContextItem
		score     float64
		snippet   string
		matchedBy []string
	}
	fused := map[string]*fusion{}

	scopesFilter := scopeSet(req.Scopes)
	kindsFilter := kindSet(req.Kinds)

	// Cache hydrated items so IDs appearing in both FTS and vector
	// results only trigger one repo.Get call.
	itemCache := map[string]domain.ContextItem{}

	// Hydrate + score FTS hits. survivingRank is 0-indexed and only
	// increments when an item passes the scope/kind filter — fHits comes
	// back UNFILTERED (FTS5 query has no scope/kind predicates), so the
	// raw range index would assign unfairly high ranks to in-scope items
	// that happened to land below out-of-scope items in BM25 order. RRF
	// contribution is 1/(survivingRank+rrfK), so the top surviving FTS
	// hit contributes 1/60.
	survivingRank := 0
	for _, h := range fHits {
		item, err := s.repo.Get(ctx, h.ID)
		if err != nil {
			continue
		}
		itemCache[h.ID] = item
		if scopesFilter != nil && !scopesFilter[item.Scope] {
			continue
		}
		if kindsFilter != nil && !kindsFilter[item.Kind] {
			continue
		}
		f, ok := fused[h.ID]
		if !ok {
			f = &fusion{item: item}
			fused[h.ID] = f
		}
		f.score += 1.0 / float64(survivingRank+rrfK)
		f.matchedBy = append(f.matchedBy, "fts")
		if f.snippet == "" {
			f.snippet = h.Snippet
		}
		survivingRank++
	}

	// Hydrate + score vector hits. Vector search has no snippet text;
	// fall back to the item title so the UI has something to show.
	// vHits is already filtered at SQL level via JOIN context_item, so
	// the defensive filter below should be a no-op — but if it ever
	// fires (race between SQL query and item update), we still want
	// post-filter rank semantics so vector and FTS contribute on equal
	// footing. Same survivingRank pattern as the FTS loop.
	survivingVecRank := 0
	for _, h := range vHits {
		item, ok := itemCache[h.ID]
		if !ok {
			var err error
			item, err = s.repo.Get(ctx, h.ID)
			if err != nil {
				continue
			}
			itemCache[h.ID] = item
		}
		if scopesFilter != nil && !scopesFilter[item.Scope] {
			continue
		}
		if kindsFilter != nil && !kindsFilter[item.Kind] {
			continue
		}
		f, ok := fused[h.ID]
		if !ok {
			f = &fusion{item: item}
			fused[h.ID] = f
		}
		f.score += 1.0 / float64(survivingVecRank+rrfK)
		f.matchedBy = append(f.matchedBy, "vector")
		if f.snippet == "" {
			f.snippet = item.Title
		}
		survivingVecRank++
	}

	out := make([]SearchResult, 0, len(fused))
	for _, f := range fused {
		out = append(out, SearchResult{
			Item: f.item, Score: f.score, Snippet: f.snippet, MatchedBy: dedupeStrings(f.matchedBy),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score > out[j].Score
		}
		// Stable tiebreak: higher ULID first = newer item first. IDs are
		// ULIDs (timestamp-prefixed, lexically sortable), so a lexical
		// descending compare is a creation-time descending compare. On a
		// score tie the more recently created item wins — for a personal
		// KB the recent note is more likely what the user wants.
		return out[i].Item.ID > out[j].Item.ID
	})
	if len(out) > limit {
		out = out[:limit]
	}
	return SearchResponse{Results: out, Total: len(out)}, nil
}

// dedupeStrings returns the unique strings in in, preserving first-seen
// order. Used for MatchedBy: an item hit by both FTS and vector ends up
// with matched_by = ["fts","vector"] not ["fts","vector","fts"].
func dedupeStrings(in []string) []string {
	if len(in) == 0 {
		return nil
	}
	seen := map[string]bool{}
	out := make([]string, 0, len(in))
	for _, s := range in {
		if seen[s] {
			continue
		}
		seen[s] = true
		out = append(out, s)
	}
	return out
}

// toStrings converts domain.Scope/Kind slices to []string for the
// VectorQuery filters. domain.Scope and domain.Kind are string-typed,
// so this is just a copy.
func toStrings[T ~string](in []T) []string {
	if len(in) == 0 {
		return nil
	}
	out := make([]string, len(in))
	for i, v := range in {
		out[i] = string(v)
	}
	return out
}

func scopeSet(s []domain.Scope) map[domain.Scope]bool {
	if len(s) == 0 {
		return nil // nil map = "all match"
	}
	m := map[domain.Scope]bool{}
	for _, v := range s {
		m[v] = true
	}
	return m
}

func kindSet(k []domain.Kind) map[domain.Kind]bool {
	if len(k) == 0 {
		return nil
	}
	m := map[domain.Kind]bool{}
	for _, v := range k {
		m[v] = true
	}
	return m
}
