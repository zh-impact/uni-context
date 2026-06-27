"""Tests for unictx.embed.ollama.OllamaEmbedder.

Mocks the httpx transport via httpx.MockTransport so no real network
is touched. The handler signature mirrors the one documented in the
task brief.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from unictx.embed.embedder import Embedder, ModelInfo
from unictx.embed.errors import EmbeddingFailed
from unictx.embed.ollama import OllamaEmbedder


def make_embedder(
    handler,
    *,
    base_url: str = "http://test.invalid",
    model: str = "bge-m3",
    dim: int = 1024,
) -> OllamaEmbedder:
    """Build an OllamaEmbedder whose httpx client uses a MockTransport.

    The embedder is constructed normally (so the default-timeout path
    runs), then we swap in a client bound to ``base_url`` with the mock
    transport. Tests can therefore assert both the production constructor
    behaviour and the round-trip behaviour.
    """
    emb = OllamaEmbedder(base_url=base_url, model=model, dimension=dim)
    transport = httpx.MockTransport(handler)
    emb._client = httpx.Client(base_url=base_url, transport=transport, timeout=60.0)
    return emb


def _ok_handler(payload: dict[str, Any]):
    """Return a handler that asserts the request shape and replies with ``payload``."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/json"
        body = json.loads(request.content)
        assert body["model"] == "bge-m3"
        assert body["input"] == ["hello", "world"]
        return httpx.Response(200, json=payload)

    return handler


# --- model() -----------------------------------------------------------------


def test_model_returns_slug_and_dimension():
    emb = OllamaEmbedder(base_url="http://x", model="bge-m3", dimension=1024)
    info = emb.model()
    assert isinstance(info, ModelInfo)
    assert info.slug == "bge-m3"
    assert info.dimension == 1024


# --- default base_url --------------------------------------------------------


def test_default_base_url_when_empty():
    emb = OllamaEmbedder(base_url="", model="bge-m3", dimension=1024)
    assert emb._base_url == "http://localhost:11434"


def test_explicit_base_url_is_respected():
    emb = OllamaEmbedder(base_url="http://ollama.svc:9000", model="x", dimension=8)
    assert emb._base_url == "http://ollama.svc:9000"


# --- embed() happy path ------------------------------------------------------


def test_embed_returns_one_vector_per_input_with_correct_dim():
    emb = make_embedder(
        _ok_handler({"embeddings": [[0.1] * 1024, [0.2] * 1024]}),
    )
    vecs = emb.embed(["hello", "world"])
    assert len(vecs) == 2
    assert len(vecs[0]) == 1024
    assert len(vecs[1]) == 1024
    assert vecs[0][0] == pytest.approx(0.1)
    assert vecs[1][0] == pytest.approx(0.2)


def test_embed_request_body_is_correct():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"embeddings": [[0.0] * 1024, [0.0] * 1024]})

    emb = make_embedder(handler)
    emb.embed(["hello", "world"])
    assert captured["path"] == "/api/embed"
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/json"
    assert captured["body"] == {"model": "bge-m3", "input": ["hello", "world"]}


# --- embed() error paths -----------------------------------------------------


def test_embed_non_200_with_error_field_includes_status_and_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    emb = make_embedder(handler)
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello", "world"])
    assert emb._model in exc_info.value.slug  # bge-m3
    assert exc_info.value.slug == "bge-m3"
    assert "ollama 404" in exc_info.value.reason
    assert "model not found" in exc_info.value.reason


def test_embed_non_200_without_error_field_returns_status_only():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"unrelated": "field"})

    emb = make_embedder(handler)
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello", "world"])
    assert exc_info.value.reason == "ollama returned 500"


def test_embed_empty_embeddings_list_raises():
    emb = make_embedder(_ok_handler({"embeddings": []}))
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello", "world"])
    assert exc_info.value.reason == "ollama returned empty embeddings"


def test_embed_mismatched_count_raises():
    # 2 inputs, only 1 embedding returned
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # echo a single embedding regardless of input count
        assert body["input"] == ["hello", "world"]
        return httpx.Response(200, json={"embeddings": [[0.0] * 1024]})

    emb = make_embedder(handler)
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello", "world"])
    assert exc_info.value.reason == "ollama returned 1 embeddings, expected 2"


def test_embed_network_error_wrapped_as_embedding_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    emb = make_embedder(handler)
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello", "world"])
    # Reason should mention the network failure in some form.
    assert "connection refused" in exc_info.value.reason


def test_embed_malformed_json_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    emb = make_embedder(handler)
    with pytest.raises(EmbeddingFailed):
        emb.embed(["hello", "world"])


# --- Protocol conformance ----------------------------------------------------


def test_ollama_embedder_satisfies_embedder_protocol():
    emb = OllamaEmbedder(base_url="http://x", model="bge-m3", dimension=1024)
    # Embedder is @runtime_checkable, so isinstance works for structural check.
    assert isinstance(emb, Embedder)
