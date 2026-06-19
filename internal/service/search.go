package service

import (
	"context"
	"fmt"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

type SearchService struct {
	searcher port.Searcher
	repo     port.ContextRepo
}

func NewSearchService(searcher port.Searcher, repo port.ContextRepo) *SearchService {
	return &SearchService{searcher: searcher, repo: repo}
}

type SearchRequest struct {
	Query  string
	Scopes []domain.Scope
	Kinds  []domain.Kind
	Limit  int
}

type SearchResult struct {
	Item    domain.ContextItem
	Score   float64
	Snippet string
}

type SearchResponse struct {
	Results []SearchResult
	Total   int
}

func (s *SearchService) Search(ctx context.Context, req SearchRequest) (SearchResponse, error) {
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
		out = append(out, SearchResult{Item: item, Score: h.Score, Snippet: h.Snippet})
	}

	return SearchResponse{Results: out, Total: len(out)}, nil
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
