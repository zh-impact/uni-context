package service

import (
	"context"
	"fmt"
	"os"
	"sort"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

type SearchService struct {
	searcher port.Searcher
	repo     port.ContextRepo
	// embedder is required for hybrid mode. When nil, a hybrid request
	// silently degrades to fts-only (Plan 1 callers are unaffected).
	embedder port.Embedder
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
func (s *SearchService) searchFTSOnly(ctx context.Context, req SearchRequest) (SearchResponse, error) {
	hits, err := s.searcher.SearchFTS(ctx, port.SearchQuery{Query: req.Query, Limit: req.Limit})
	if err != nil {
		return SearchResponse{}, fmt.Errorf("fts: %w", err)
	}

	scopes := scopeSet(req.Scopes)
	kinds := kindSet(req.Kinds)

	var out []SearchResult
	for _, h := range hits {
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

// searchHybrid runs FTS and vector KNN concurrently in sequence, then
// fuses via RRF. Over-fetches 3×limit from each path so post-filter
// trimming still yields req.Limit results. The fused map is trimmed to
// req.Limit at the end (VectorStore pre-trims to 3×limit, not to limit).
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

	vHits, err := s.searcher.SearchVector(ctx, port.VectorQuery{
		Vector: queryVec[0], Model: s.embedder.Model().Slug,
		Limit: overFetch, Scopes: scopes, Kinds: kinds,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "warn: hybrid search vector lookup failed, falling back to fts-only: %v\n", err)
		return s.searchFTSOnly(ctx, req)
	}

	fHits, err := s.searcher.SearchFTS(ctx, port.SearchQuery{Query: req.Query, Limit: overFetch})
	if err != nil {
		return SearchResponse{}, fmt.Errorf("fts search: %w", err)
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

	// Hydrate + score FTS hits. rank is 0-indexed; contribution uses
	// 1/(rank+rrfK) so the top FTS hit contributes 1/60.
	for rank, h := range fHits {
		item, err := s.repo.Get(ctx, h.ID)
		if err != nil {
			continue
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
		f.score += 1.0 / float64(rank+rrfK)
		f.matchedBy = append(f.matchedBy, "fts")
		if f.snippet == "" {
			f.snippet = h.Snippet
		}
	}

	// Hydrate + score vector hits. Vector search has no snippet text;
	// fall back to the item title so the UI has something to show.
	for rank, h := range vHits {
		item, err := s.repo.Get(ctx, h.ID)
		if err != nil {
			continue
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
		f.score += 1.0 / float64(rank+rrfK)
		f.matchedBy = append(f.matchedBy, "vector")
		if f.snippet == "" {
			f.snippet = item.Title
		}
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
		// Stable tiebreak: lower ID first so test output is deterministic.
		return out[i].Item.ID < out[j].Item.ID
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
