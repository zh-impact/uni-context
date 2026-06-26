package fake

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestFakeEmbedder_DeterministicByContent(t *testing.T) {
	e := New("fake-slug", 8)

	v1, err := e.Embed(context.Background(), []string{"hello world"})
	require.NoError(t, err)
	require.Len(t, v1, 1)
	assert.Len(t, v1[0], 8, "dimension must match Model().Dimension")

	// Same input → same output (deterministic, so tests are reproducible)
	v2, _ := e.Embed(context.Background(), []string{"hello world"})
	assert.Equal(t, v1[0], v2[0])

	// Different input → different output
	v3, _ := e.Embed(context.Background(), []string{"different"})
	assert.NotEqual(t, v1[0], v3[0])
}

func TestFakeEmbedder_BatchPreservesOrder(t *testing.T) {
	e := New("fake", 4)
	out, err := e.Embed(context.Background(), []string{"a", "b", "c"})
	require.NoError(t, err)
	require.Len(t, out, 3)
	// Each result must match the per-text embedding
	for i, text := range []string{"a", "b", "c"} {
		single, _ := e.Embed(context.Background(), []string{text})
		assert.Equal(t, single[0], out[i], "batch index %d", i)
	}
}

func TestFakeEmbedder_ModelInfo(t *testing.T) {
	e := New("fake-slug", 16)
	m := e.Model()
	assert.Equal(t, "fake-slug", m.Slug)
	assert.Equal(t, 16, m.Dimension)
}
