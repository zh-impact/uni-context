package port

import "context"

// SearchQuery defines a full-text search.
type SearchQuery struct {
    Query   string
    Limit   int
    // Future: filter by scope/kind/tags via FTS WHERE — added in service.Search wrapper
}

// SearchHit is one BM25 search result.
type SearchHit struct {
    ID       string
    Score    float64
    Snippet  string
}

// Searcher does keyword search (BM25 via FTS5 in this plan).
type Searcher interface {
    SearchFTS(ctx context.Context, q SearchQuery) ([]SearchHit, error)
}
