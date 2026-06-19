package port

import "context"

// SearchQuery defines a full-text search.
type SearchQuery struct {
	Query string
	Limit int
	// Future: filter by scope/kind/tags via FTS WHERE — added in service.Search wrapper
}

// SearchHit is one BM25 search result.
type SearchHit struct {
	ID      string
	Score   float64
	Snippet string
}

// Searcher does keyword search (BM25 via FTS5) and, when the backing
// store has a vector index, KNN vector search. Implementations may
// delegate SearchVector to a separate VectorStore (see sqlite.Searcher,
// which composes both).
type Searcher interface {
	SearchFTS(ctx context.Context, q SearchQuery) ([]SearchHit, error)
	// SearchVector runs KNN against the searcher's backing vector store.
	// Returns hits ordered by Score DESC. VectorQuery, VectorHit and the
	// filters they carry are defined in vectorstore.go (same package).
	SearchVector(ctx context.Context, q VectorQuery) ([]VectorHit, error)
}
