package openai

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
	var (
		gotPath   string
		gotMethod string
		gotAuth   string
		gotBody   map[string]any
		gotCalls  int
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotCalls++
		gotPath = r.URL.Path
		gotMethod = r.Method
		gotAuth = r.Header.Get("Authorization")
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		// Echo back a 1024-dim vector per input. OpenAI shape:
		//   {"data": [{"embedding": [...], "index": 0}, ...], "model": ..., "usage": {...}}
		inputs := gotBody["input"].([]any)
		out := make([]map[string]any, len(inputs))
		for i := range inputs {
			v := make([]float32, 1024)
			v[0] = float32(i + 1)
			out[i] = map[string]any{"embedding": v, "index": i}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"data":  out,
			"model": gotBody["model"],
		})
	}))
	defer srv.Close()

	// baseURL passed by the caller includes the "/v1" prefix, mirroring
	// how a user would write `base_url: http://localhost:1234/v1` in
	// config.yaml. The adapter appends "/embeddings" to this.
	e := New(srv.URL+"/v1", "bge-m3", 1024, "sk-test-key")
	assert.Equal(t, "bge-m3", e.Model().Slug)
	assert.Equal(t, 1024, e.Model().Dimension)

	vecs, err := e.Embed(context.Background(), []string{"hello", "world"})
	require.NoError(t, err)
	require.Len(t, vecs, 2)
	require.Len(t, vecs[0], 1024)
	assert.Equal(t, float32(1), vecs[0][0])
	assert.Equal(t, float32(2), vecs[1][0])

	// Request shape
	assert.Equal(t, "/v1/embeddings", gotPath)
	assert.Equal(t, http.MethodPost, gotMethod)
	assert.Equal(t, "Bearer sk-test-key", gotAuth)
	assert.Equal(t, "bge-m3", gotBody["model"])
	assert.Equal(t, []any{"hello", "world"}, gotBody["input"])
	assert.Equal(t, 1, gotCalls, "single HTTP round-trip for batch")
}

func TestEmbedder_Unit_NoAPIKeyOmitsAuthHeader(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		out := []map[string]any{{
			"embedding": make([]float32, 8), "index": 0,
		}}
		_ = json.NewEncoder(w).Encode(map[string]any{"data": out})
	}))
	defer srv.Close()

	e := New(srv.URL+"/v1", "bge-m3", 8, "") // LMStudio: no key
	_, err := e.Embed(context.Background(), []string{"hi"})
	require.NoError(t, err)
	assert.Empty(t, gotAuth, "no Authorization header when API key is empty (LMStudio compat)")
}

func TestEmbedder_Unit_PropagatesHTTPError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"error":{"message":"model bge-m3 not loaded","type":"invalid_request_error"}}`))
	}))
	defer srv.Close()

	e := New(srv.URL+"/v1", "bge-m3", 1024, "")
	_, err := e.Embed(context.Background(), []string{"hi"})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "404")
}

func TestEmbedder_Unit_EmptyDataIsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"data": []any{}})
	}))
	defer srv.Close()

	e := New(srv.URL+"/v1", "bge-m3", 1024, "")
	_, err := e.Embed(context.Background(), []string{"hi"})
	require.Error(t, err)
}

func TestEmbedder_Unit_MismatchedCountIsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Asked for 2 embeddings, server returns 1
		out := []map[string]any{{
			"embedding": make([]float32, 8), "index": 0,
		}}
		_ = json.NewEncoder(w).Encode(map[string]any{"data": out})
	}))
	defer srv.Close()

	e := New(srv.URL+"/v1", "bge-m3", 8, "")
	_, err := e.Embed(context.Background(), []string{"a", "b"})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "1 embeddings")
}

func TestEmbedder_Unit_RespectsRequestContext(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done() // block until client cancels
	}))
	defer srv.Close()

	e := New(srv.URL+"/v1", "bge-m3", 8, "")
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // pre-cancel

	_, err := e.Embed(ctx, []string{"hi"})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "context")
}
