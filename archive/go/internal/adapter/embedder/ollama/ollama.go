// Package ollama is a minimal net/http client for Ollama's /api/embed
// endpoint. No SDK dependency. Default base URL http://localhost:11434.
package ollama

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
	baseURL string
	model   string
	dim     int
	client  *http.Client
}

func New(baseURL, model string, dimension int) *Embedder {
	if baseURL == "" {
		baseURL = "http://localhost:11434"
	}
	return &Embedder{
		baseURL: baseURL,
		model:   model,
		dim:     dimension,
		client:  &http.Client{Timeout: 60 * time.Second},
	}
}

func (e *Embedder) Model() port.ModelInfo {
	return port.ModelInfo{Slug: e.model, Dimension: e.dim}
}

type embedReq struct {
	Model string   `json:"model"`
	Input []string `json:"input"`
}

type embedResp struct {
	Embeddings [][]float32 `json:"embeddings"`
	Error      string      `json:"error,omitempty"`
}

func (e *Embedder) Embed(ctx context.Context, texts []string) ([][]float32, error) {
	body, err := json.Marshal(embedReq{Model: e.model, Input: texts})
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.baseURL+"/api/embed", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := e.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("call ollama: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		var r embedResp
		_ = json.NewDecoder(resp.Body).Decode(&r)
		if r.Error != "" {
			return nil, fmt.Errorf("ollama %d: %s", resp.StatusCode, r.Error)
		}
		return nil, fmt.Errorf("ollama returned %d", resp.StatusCode)
	}

	var r embedResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}
	if len(r.Embeddings) == 0 {
		return nil, fmt.Errorf("ollama returned empty embeddings")
	}
	if len(r.Embeddings) != len(texts) {
		return nil, fmt.Errorf("ollama returned %d embeddings, expected %d",
			len(r.Embeddings), len(texts))
	}
	return r.Embeddings, nil
}
