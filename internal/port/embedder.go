package port

import "context"

// ModelInfo identifies an embedding model. Slug matches the
// embedding_model.slug column; Dimension matches the vec0 table's
// FLOAT[n] declaration.
type ModelInfo struct {
	Slug      string
	Dimension int
}

// Embedder produces vector embeddings for text inputs. Implementations
// must be safe for concurrent use.
//
// Batch semantics: Embed receives multiple texts in one call and
// returns one vector per input, in order. Implementations backed by a
// single-input API (e.g. legacy Ollama /api/embeddings) loop internally.
type Embedder interface {
	// Model returns the slug + dimension this embedder produces.
	Model() ModelInfo
	// Embed converts texts to vectors. len(output) MUST equal len(texts).
	Embed(ctx context.Context, texts []string) ([][]float32, error)
}
