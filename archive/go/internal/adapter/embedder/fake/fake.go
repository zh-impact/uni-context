// Package fake provides a deterministic Embedder for tests. Vectors are
// derived from sha256(text) — same input always produces the same vector,
// different inputs produce uncorrelated vectors. No external dependency.
package fake

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"fmt"

	"uni-context/internal/port"
)

type Embedder struct {
	slug      string
	dimension int
	// embedHook, when non-nil, overrides Embed. Tests use it to capture
	// inputs, inject errors, or return canned vectors. nil = default
	// deterministic behavior (vectorFor via sha256).
	embedHook func(texts []string) ([][]float32, error)
}

func New(slug string, dimension int) *Embedder {
	return &Embedder{slug: slug, dimension: dimension}
}

func (e *Embedder) Model() port.ModelInfo {
	return port.ModelInfo{Slug: e.slug, Dimension: e.dimension}
}

// SetEmbedHook lets tests override Embed behavior: capture inputs, inject
// errors, return canned vectors. Pass nil to reset to default behavior.
// Default behavior (hook nil) is unchanged so existing tests keep their
// deterministic sha256-derived vectors.
func (e *Embedder) SetEmbedHook(fn func([]string) ([][]float32, error)) {
	e.embedHook = fn
}

func (e *Embedder) Embed(_ context.Context, texts []string) ([][]float32, error) {
	if e.embedHook != nil {
		return e.embedHook(texts)
	}
	out := make([][]float32, len(texts))
	for i, text := range texts {
		out[i] = e.vectorFor(text)
	}
	return out, nil
}

// vectorFor produces a deterministic float32 vector. The pseudo-random
// bytes come from sha256(text|i) for each component. Values are scaled
// to [-1, 1). Not real embeddings and not L2-normalized — just stable
// and uncorrelated across inputs, which is what tests need.
func (e *Embedder) vectorFor(text string) []float32 {
	v := make([]float32, e.dimension)
	for i := 0; i < e.dimension; i++ {
		h := sha256.Sum256([]byte(fmt.Sprintf("%s|%d", text, i)))
		u := binary.LittleEndian.Uint32(h[:4])
		v[i] = float32(int32(u)) / float32(1<<31) // [-1, 1)
	}
	return v
}
