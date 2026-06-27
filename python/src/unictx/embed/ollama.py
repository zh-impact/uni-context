"""Ollama /api/embed HTTP client.

Faithful port of Go's internal/adapter/embedder/ollama/ollama.go. No
SDK dependency: a single httpx.Client posts JSON to ``<base_url>/api/embed``.

Adaptations vs Go (per Plan §Python Conventions):
- ctx dropped (Python is sync; the Protocol has no ctx param).
- snake_case attribute and method names.
- Errors raised as :class:`unictx.embed.errors.EmbeddingFailed` (carrying
  the model slug) instead of returned as ``(nil, err)``.
- Floats are Python ``float`` (== C double). Ollama emits JSON numbers,
  which Python parses as float64. vec0's serialize_float32 downcasts to
  float32 immediately before storage, so this matches wire reality and
  does not affect what hits the index.
- Timeout is 60s (Go is authoritative for faithful-port; the task brief's
  "30s" is a documentation typo).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from unictx.embed.embedder import ModelInfo
from unictx.embed.errors import EmbeddingFailed

_DEFAULT_BASE_URL = "http://localhost:11434"
_TIMEOUT_SECONDS = 60.0


class OllamaEmbedder:
    """Embedder backed by Ollama's ``POST /api/embed`` endpoint.

    Construct with ``base_url`` (defaults to localhost:11434 when empty),
    ``model`` slug, and the expected ``dimension``. The dimension is
    declarative — Ollama does not return it on the embed response — and
    is used to populate :class:`ModelInfo` for the storage layer.

    The instance owns an :class:`httpx.Client`. Tests may replace
    ``self._client`` with one wired to :class:`httpx.MockTransport`.
    """

    def __init__(self, *, base_url: str, model: str, dimension: int) -> None:
        # Mirror Go's empty-string default. An explicitly-empty base_url
        # means "use the well-known local daemon".
        self._base_url: str = base_url if base_url else _DEFAULT_BASE_URL
        self._model: str = model
        self._dimension: int = dimension
        self._client: httpx.Client = httpx.Client(timeout=_TIMEOUT_SECONDS)

    # -- Embedder protocol ---------------------------------------------------

    def model(self) -> ModelInfo:
        """Return the configured slug + dimension (no remote call)."""
        return ModelInfo(slug=self._model, dimension=self._dimension)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """POST texts to /api/embed, return one vector per input.

        Raises :class:`EmbeddingFailed` on:
        - Non-2xx HTTP status (reason includes ollama's ``error`` field
          when present, else the bare status code).
        - Empty ``embeddings`` array in the response body.
        - Count mismatch between inputs and returned embeddings.
        - JSON decode failure on either error or success path.
        - Any :class:`httpx.HTTPError` from the transport.
        """
        endpoint = f"{self._base_url}/api/embed"
        body = json.dumps({"model": self._model, "input": texts})
        headers = {"Content-Type": "application/json"}

        try:
            response = self._client.post(endpoint, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise EmbeddingFailed(self._model, f"call ollama: {exc}") from exc

        if response.status_code != 200:
            reason = self._extract_error_reason(response)
            raise EmbeddingFailed(self._model, reason)

        try:
            payload = response.json()
        except ValueError as exc:
            # ValueError covers json.JSONDecodeError (its subclass) plus
            # any other decode-time ValueError from the JSON parser.
            raise EmbeddingFailed(self._model, f"decode response: {exc}") from exc

        embeddings: list[list[float]] = payload.get("embeddings") or []
        if len(embeddings) == 0:
            raise EmbeddingFailed(self._model, "ollama returned empty embeddings")
        if len(embeddings) != len(texts):
            raise EmbeddingFailed(
                self._model,
                f"ollama returned {len(embeddings)} embeddings, expected {len(texts)}",
            )
        return embeddings

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _extract_error_reason(response: httpx.Response) -> str:
        """Mirror Go's two-arm error formatter for non-200 responses.

        Tries to decode ``{"error": "..."}``. If the field is present and
        non-empty, returns ``f"ollama {status}: {error}"``; otherwise
        returns ``f"ollama returned {status}"``. JSON-decode failures
        fall through to the bare-status form (matching Go, which ignores
        the decoder error).
        """
        error_text = ""
        try:
            payload: dict[str, Any] = response.json()
            error_text = str(payload.get("error") or "")
        except ValueError:
            # Malformed body — fall through to the bare-status reason.
            # json.JSONDecodeError is a ValueError subclass, so this
            # covers both JSON failures and any other decode ValueError.
            pass

        if error_text:
            return f"ollama {response.status_code}: {error_text}"
        return f"ollama returned {response.status_code}"
