"""Tests for unictx.embed.openai.OpenAIEmbedder.

Mocks the httpx transport via httpx.MockTransport so no real network
is touched. Mirrors the Ollama test structure; the OpenAI-specific
twists are:

- Optional ``Authorization`` header (omitted when ``api_key=""`` to
  support local LMStudio).
- Response shape ``{"data": [{"embedding": [...], "index": int}, ...]}``
  with defensive sort by ``index``.
- Polymorphic ``error`` field on the failure envelope: object form
  ``{"message": "...", "type": "..."}`` OR bare string ``"..."``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from unictx.embed.embedder import Embedder, ModelInfo
from unictx.embed.errors import EmbeddingFailed
from unictx.embed.openai import OpenAIEmbedder


def make_embedder(
    handler,
    *,
    base_url: str = "http://test.invalid/v1",
    model: str = "bge-m3",
    dim: int = 1024,
    api_key: str = "",
) -> OpenAIEmbedder:
    """Build an OpenAIEmbedder whose httpx client uses a MockTransport.

    The embedder is constructed normally (so the production constructor
    behaviour runs), then we swap in a client bound to ``base_url`` with
    the mock transport. Tests can therefore assert both the production
    constructor behaviour and the round-trip behaviour.
    """
    emb = OpenAIEmbedder(base_url=base_url, model=model, dimension=dim, api_key=api_key)
    transport = httpx.MockTransport(handler)
    emb._client = httpx.Client(base_url=base_url, transport=transport, timeout=60.0)
    return emb


def _ok_handler(payload: dict[str, Any], *, expected_auth: str | None = None):
    """Return a handler that asserts request shape and replies with ``payload``.

    ``expected_auth``:
    - ``None``  -> assert no Authorization header is present.
    - ``"..."`` -> assert that exact bearer token string.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/json"
        if expected_auth is None:
            assert "authorization" not in request.headers, (
                "no Authorization header expected when api_key is empty"
            )
        else:
            assert request.headers["authorization"] == expected_auth
        body = json.loads(request.content)
        assert body["model"] == "bge-m3"
        assert body["input"] == ["hello", "world"]
        return httpx.Response(200, json=payload)

    return handler


# --- model() -----------------------------------------------------------------


def test_model_returns_slug_and_dimension():
    emb = OpenAIEmbedder(base_url="http://x/v1", model="bge-m3", dimension=1024, api_key="k")
    info = emb.model()
    assert isinstance(info, ModelInfo)
    assert info.slug == "bge-m3"
    assert info.dimension == 1024


# --- embed() happy path ------------------------------------------------------


def test_embed_returns_one_vector_per_input_with_correct_dim():
    emb = make_embedder(
        _ok_handler(
            {
                "data": [
                    {"embedding": [0.1] * 1024, "index": 0},
                    {"embedding": [0.2] * 1024, "index": 1},
                ]
            }
        ),
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
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.0] * 1024, "index": 0},
                    {"embedding": [0.0] * 1024, "index": 1},
                ]
            },
        )

    emb = make_embedder(handler)
    emb.embed(["hello", "world"])
    assert captured["path"] == "/v1/embeddings"
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/json"
    assert captured["body"] == {"model": "bge-m3", "input": ["hello", "world"]}


# --- Authorization header behaviour ------------------------------------------


def test_authorization_header_present_when_api_key_set():
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.0] * 8, "index": 0},
                    {"embedding": [0.0] * 8, "index": 1},
                ]
            },
        )

    emb = make_embedder(handler, api_key="sk-test-key")
    emb.embed(["hello", "world"])
    assert seen_auth == ["Bearer sk-test-key"]


def test_authorization_header_omitted_when_api_key_empty():
    seen_auth_header: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx lowercases header names; absence -> None via .get().
        seen_auth_header.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.0] * 8, "index": 0},
                    {"embedding": [0.0] * 8, "index": 1},
                ]
            },
        )

    emb = make_embedder(handler, api_key="")
    emb.embed(["hello", "world"])
    assert seen_auth_header == [None], (
        "LMStudio local mode: no Authorization header when api_key is empty"
    )


# --- embed() error paths: status + error envelope ---------------------------


def test_embed_non_200_with_object_error_includes_status_and_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "message": "model not found",
                    "type": "invalid_request_error",
                }
            },
        )

    emb = make_embedder(handler)
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello", "world"])
    assert exc_info.value.slug == "bge-m3"
    assert exc_info.value.reason == "openai-compat 404: model not found"


def test_embed_non_200_with_bare_string_error_includes_status_and_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "model is loading"})

    emb = make_embedder(handler)
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello", "world"])
    assert exc_info.value.reason == "openai-compat 503: model is loading"


def test_embed_non_200_without_error_field_returns_status_only():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"unrelated": "field"})

    emb = make_embedder(handler)
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello", "world"])
    assert exc_info.value.reason == "openai-compat returned 500"


# --- embed() error paths: data validation ------------------------------------


def test_embed_empty_data_list_raises():
    emb = make_embedder(_ok_handler({"data": []}))
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello", "world"])
    assert exc_info.value.reason == "openai-compat returned empty embeddings"


def test_embed_count_mismatch_raises():
    # 1 input but server returns 2 data items
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["input"] == ["hello"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.0] * 8, "index": 0},
                    {"embedding": [0.0] * 8, "index": 1},
                ]
            },
        )

    emb = make_embedder(handler)
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello"])
    assert exc_info.value.reason == ("openai-compat returned 2 embeddings, expected 1")


def test_embed_defensive_sort_by_index():
    """Server returns data out-of-order; output must follow index, not array order."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    # index 1 first in the array
                    {"embedding": [0.9] * 8, "index": 1},
                    # index 0 second in the array
                    {"embedding": [0.1] * 8, "index": 0},
                ]
            },
        )

    emb = make_embedder(handler)
    vecs = emb.embed(["hello", "world"])
    assert vecs[0][0] == pytest.approx(0.1), "index 0 must be first"
    assert vecs[1][0] == pytest.approx(0.9), "index 1 must be second"


def test_embed_out_of_range_index_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.0] * 8, "index": 0},
                    {"embedding": [0.0] * 8, "index": 5},
                ]
            },
        )

    emb = make_embedder(handler)
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello", "world"])
    assert "out-of-range index 5" in exc_info.value.reason


# --- embed() error paths: transport + decode ---------------------------------


def test_embed_network_error_wrapped_as_embedding_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    emb = make_embedder(handler)
    with pytest.raises(EmbeddingFailed) as exc_info:
        emb.embed(["hello", "world"])
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


def test_openai_embedder_satisfies_embedder_protocol():
    emb = OpenAIEmbedder(base_url="http://x/v1", model="bge-m3", dimension=1024, api_key="")
    # Embedder is @runtime_checkable, so isinstance works for structural check.
    assert isinstance(emb, Embedder)
