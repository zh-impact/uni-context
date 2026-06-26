"""Embedder Protocol + ModelInfo.

Ports Go's internal/port/embedder.go. Adaptations per Plan
§Python Conventions: ctx params dropped, snake_case methods,
`@dataclass(slots=True)` for the value type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class ModelInfo:
    """Identifies an embedding model. Mirrors Go's port.ModelInfo.

    slug matches the embedding_model.slug column; dimension matches the
    vec0 table's FLOAT[n] declaration.
    """

    slug: str = ""
    dimension: int = 0


@runtime_checkable
class Embedder(Protocol):
    """Produces vector embeddings for text inputs. Mirrors Go's port.Embedder.

    Implementations must be safe for concurrent use.

    Batch semantics: `embed` receives multiple texts in one call and
    returns one vector per input, in order. Implementations backed by a
    single-input API (e.g. legacy Ollama /api/embeddings) loop
    internally.
    """

    def model(self) -> ModelInfo:
        """Return the slug + dimension this embedder produces."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Convert texts to vectors. len(output) MUST equal len(texts).

        Raises EmbeddingFailed on backend error (HTTP 5xx, network
        failure, malformed response). The caller is responsible for
        retry policy.
        """
        ...
