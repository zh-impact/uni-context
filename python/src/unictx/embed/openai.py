"""OpenAI-compatible /v1/embeddings HTTP client.

Faithful port of Go's internal/adapter/embedder/openai/openai.go. No
SDK dependency: a single httpx.Client posts JSON to
``<base_url>/embeddings``. The ``/v1`` prefix is the caller's
responsibility — it lives in ``base_url`` (e.g.
``http://localhost:1234/v1`` for LMStudio, ``https://api.openai.com/v1``
for hosted OpenAI). The adapter only appends ``/embeddings``.

Adaptations vs Go (per Plan §Python Conventions):
- ctx dropped (Python is sync; the Protocol has no ctx param).
- snake_case attribute and method names.
- Errors raised as :class:`unictx.embed.errors.EmbeddingFailed`
  (carrying the model slug) instead of returned as ``(nil, err)``.
- Floats are Python ``float`` (== C double). vec0's serialize_float32
  downcasts to float32 immediately before storage, so this matches wire
  reality and does not affect what hits the index.
- Timeout is 60s (Go is authoritative for faithful-port).

LMStudio local mode: pass ``api_key=""`` (the default) to omit the
``Authorization`` header entirely. Pass ``api_key="sk-..."`` for hosted
OpenAI / vLLM with auth enabled.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from unictx.embed.embedder import ModelInfo
from unictx.embed.errors import EmbeddingFailed

_TIMEOUT_SECONDS = 60.0


class OpenAIEmbedder:
    """Embedder backed by an OpenAI-compatible ``POST /embeddings`` endpoint.

    Construct with ``base_url`` (must already include ``/v1``),
    ``model`` slug, the expected ``dimension``, and an optional
    ``api_key``. An empty ``api_key`` omits the ``Authorization`` header
    entirely, which is what local servers like LMStudio expect; a
    non-empty key is sent as ``Authorization: Bearer <key>`` for hosted
    OpenAI or authenticated vLLM.

    The instance owns an :class:`httpx.Client`. Tests may replace
    ``self._client`` with one wired to :class:`httpx.MockTransport`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dimension: int,
        api_key: str = "",
    ) -> None:
        self._base_url: str = base_url
        self._model: str = model
        self._dimension: int = dimension
        self._api_key: str = api_key
        self._client: httpx.Client = httpx.Client(timeout=_TIMEOUT_SECONDS)

    # -- Embedder protocol ---------------------------------------------------

    def model(self) -> ModelInfo:
        """Return the configured slug + dimension (no remote call)."""
        return ModelInfo(slug=self._model, dimension=self._dimension)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """POST texts to ``<base_url>/embeddings``, return one vector per input.

        The output is sorted defensively by the response's ``index``
        field — the OpenAI spec returns data in input order but does not
        formally guarantee it, so we follow ``index`` to be safe against
        a misbehaving server silently swapping vectors.

        Raises :class:`EmbeddingFailed` on:
        - Non-2xx HTTP status (reason includes the server's ``error``
          field when present, else the bare status code).
        - Empty ``data`` array in the response body.
        - Count mismatch between inputs and returned data items.
        - Out-of-range ``index`` value in a data item.
        - JSON decode failure on either error or success path.
        - Any :class:`httpx.HTTPError` from the transport.
        """
        endpoint = f"{self._base_url}/embeddings"
        body = json.dumps({"model": self._model, "input": texts})
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            response = self._client.post(endpoint, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise EmbeddingFailed(self._model, f"call openai-compat: {exc}") from exc

        if response.status_code != 200:
            reason = self._extract_error_reason(response)
            raise EmbeddingFailed(self._model, reason)

        try:
            payload = response.json()
        except ValueError as exc:
            # ValueError covers json.JSONDecodeError (its subclass) plus
            # any other decode-time ValueError from the JSON parser.
            raise EmbeddingFailed(self._model, f"decode response: {exc}") from exc

        return self._build_embeddings(payload, expected_count=len(texts))

    # -- internals -----------------------------------------------------------

    def _build_embeddings(self, payload: Any, *, expected_count: int) -> list[list[float]]:
        """Validate the success-path response and return vectors in index order.

        Mirrors Go's post-decode validation: empty data, count mismatch,
        and out-of-range index all surface as :class:`EmbeddingFailed`.
        """
        data: list[dict[str, Any]] = payload.get("data") or []
        if len(data) == 0:
            # Some OpenAI-compat servers (notably LMStudio during model
            # loading) return 200 OK with an ``error`` field instead of
            # data. Surface that message when present, matching Go.
            msg = _extract_error_message(payload)
            if msg:
                raise EmbeddingFailed(
                    self._model,
                    f"openai-compat returned empty embeddings: {msg}",
                )
            raise EmbeddingFailed(self._model, "openai-compat returned empty embeddings")
        if len(data) != expected_count:
            raise EmbeddingFailed(
                self._model,
                (f"openai-compat returned {len(data)} embeddings, expected {expected_count}"),
            )

        # Defensive sort by index: OpenAI's spec returns data in input
        # order but doesn't formally guarantee it. Cheap insurance
        # against a misbehaving server silently swapping vectors.
        out: list[list[float] | None] = [None] * len(data)
        for item in data:
            index = item["index"]
            if index < 0 or index >= len(out):
                raise EmbeddingFailed(
                    self._model,
                    f"openai-compat returned out-of-range index {index}",
                )
            out[index] = list(item["embedding"])
        # ``out`` is fully populated: count matched and every index was
        # in range, so no slot is still None.
        return out  # type: ignore[return-value]

    @staticmethod
    def _extract_error_reason(response: httpx.Response) -> str:
        """Mirror Go's two-arm error formatter for non-200 responses.

        Tries to decode the response body as JSON and extract an
        ``error`` field (object form ``{"message": ..., "type": ...}``
        OR bare string). If a message is found, returns
        ``f"openai-compat {status}: {msg}"``; otherwise returns
        ``f"openai-compat returned {status}"``. JSON-decode failures
        fall through to the bare-status form (matching Go, which ignores
        the decoder error).
        """
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            return f"openai-compat returned {response.status_code}"

        msg = _extract_error_message(payload)
        if msg:
            return f"openai-compat {response.status_code}: {msg}"
        return f"openai-compat returned {response.status_code}"


def _extract_error_message(payload: dict[str, Any]) -> str:
    """Extract a human-readable message from an OpenAI error field.

    Ports Go's ``errorMessage`` helper. Accepts EITHER the canonical
    object form ``{"message": "...", "type": "..."}`` OR a bare string
    ``"..."``. Returns ``""`` when the field is absent, ``None``, or in
    any other shape.

    Real OpenAI-compat servers — notably LMStudio during model loading
    or transient internal errors — sometimes return ``error`` as a bare
    string instead of the spec'd object. This helper keeps the overall
    response decode successful regardless of which shape the server used.
    """
    raw = payload.get("error")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return str(raw.get("message") or "")
    return ""
