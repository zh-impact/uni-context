// Package openai is a minimal net/http client for the OpenAI-compatible
// /v1/embeddings endpoint. Works with LMStudio (local, no API key),
// OpenAI itself (api.openai.com, requires key), vLLM, and any other
// server that implements the OpenAI embeddings contract.
//
// This is the OpenAI-compat path deferred from Plan 2a as Plan 2d; it
// ships early so users without Ollama (e.g. LMStudio users) can exercise
// the Plan 2a hybrid-search path.
package openai

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"uni-context/internal/port"
)

type Embedder struct {
	baseURL string // e.g. "http://localhost:1234/v1" or "https://api.openai.com/v1"
	apiKey  string // empty = no Authorization header (LMStudio local)
	model   string
	dim     int
	client  *http.Client
}

// New constructs an OpenAI-compat embedder. baseURL is required and must
// already include the "/v1" prefix (callers pass it verbatim). apiKey
// is optional — empty string omits the Authorization header entirely,
// which is what local servers like LMStudio expect.
func New(baseURL, model string, dimension int, apiKey string) *Embedder {
	return &Embedder{
		baseURL: baseURL,
		apiKey:  apiKey,
		model:   model,
		dim:     dimension,
		client:  &http.Client{Timeout: 60 * time.Second},
	}
}

func (e *Embedder) Model() port.ModelInfo {
	return port.ModelInfo{Slug: e.model, Dimension: e.dim}
}

// embedReq matches the OpenAI embeddings request shape.
// `input` may be a single string or an array of strings; we always send
// an array so a batched call produces a deterministic response shape.
type embedReq struct {
	Model string   `json:"model"`
	Input []string `json:"input"`
}

// embedResp captures the fields we read from the OpenAI embeddings
// response. The `data` array's per-item shape is `{embedding, index}`;
// we sort by `index` defensively because the spec doesn't guarantee
// array order, though every observed implementation preserves it.
type embedResp struct {
	Data []struct {
		Embedding []float32 `json:"embedding"`
		Index     int       `json:"index"`
	} `json:"data"`
	// Error mirrors OpenAI's error envelope: `{"error": {"message": ...}}`.
	// We only read it on non-200 responses for diagnostic context.
	Error *struct {
		Message string `json:"message"`
		Type    string `json:"type"`
	} `json:"error,omitempty"`
}

func (e *Embedder) Embed(ctx context.Context, texts []string) ([][]float32, error) {
	body, err := json.Marshal(embedReq{Model: e.model, Input: texts})
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.baseURL+"/embeddings", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if e.apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+e.apiKey)
	}

	resp, err := e.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("call openai-compat: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		var r embedResp
		_ = json.NewDecoder(resp.Body).Decode(&r)
		if r.Error != nil && r.Error.Message != "" {
			return nil, fmt.Errorf("openai-compat %d: %s", resp.StatusCode, r.Error.Message)
		}
		return nil, fmt.Errorf("openai-compat returned %d", resp.StatusCode)
	}

	var r embedResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}
	if len(r.Data) == 0 {
		return nil, fmt.Errorf("openai-compat returned empty embeddings")
	}
	if len(r.Data) != len(texts) {
		return nil, fmt.Errorf("openai-compat returned %d embeddings, expected %d",
			len(r.Data), len(texts))
	}

	// Defensive sort by index: OpenAI's spec returns data in input order
	// but doesn't formally guarantee it. Cheap insurance against a
	// misbehaving server silently swapping vectors.
	out := make([][]float32, len(r.Data))
	for _, d := range r.Data {
		if d.Index < 0 || d.Index >= len(out) {
			return nil, fmt.Errorf("openai-compat returned out-of-range index %d", d.Index)
		}
		out[d.Index] = d.Embedding
	}
	return out, nil
}
