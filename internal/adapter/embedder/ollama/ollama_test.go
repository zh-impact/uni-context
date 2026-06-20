package ollama

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestEmbedder_Unit_HTTPRoundTrip(t *testing.T) {
	var gotReq map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/api/embed", r.URL.Path)
		require.Equal(t, http.MethodPost, r.Method)
		_ = json.NewDecoder(r.Body).Decode(&gotReq)
		// Echo back a 1024-dim vector per input
		inputs := gotReq["input"].([]any)
		out := make([][]float32, len(inputs))
		for i := range inputs {
			v := make([]float32, 1024)
			v[0] = float32(i + 1)
			out[i] = v
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"embeddings": out})
	}))
	defer srv.Close()

	e := New(srv.URL, "bge-m3", 1024)
	assert.Equal(t, "bge-m3", e.Model().Slug)
	assert.Equal(t, 1024, e.Model().Dimension)

	vecs, err := e.Embed(context.Background(), []string{"hello", "world"})
	require.NoError(t, err)
	require.Len(t, vecs, 2)
	require.Len(t, vecs[0], 1024)
	assert.Equal(t, float32(1), vecs[0][0])
	assert.Equal(t, float32(2), vecs[1][0])

	// Request shape
	assert.Equal(t, "bge-m3", gotReq["model"])
	assert.Equal(t, []any{"hello", "world"}, gotReq["input"])
}

func TestEmbedder_Unit_PropagatesHTTPError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"error":"model 'bge-m3' not found, try pulling it first"}`))
	}))
	defer srv.Close()

	e := New(srv.URL, "bge-m3", 1024)
	_, err := e.Embed(context.Background(), []string{"hi"})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "404")
}

func TestEmbedder_Unit_EmptyResponseIsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"embeddings": []any{}})
	}))
	defer srv.Close()

	e := New(srv.URL, "bge-m3", 1024)
	_, err := e.Embed(context.Background(), []string{"hi"})
	require.Error(t, err)
}
