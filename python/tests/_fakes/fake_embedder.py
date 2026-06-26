"""FakeEmbedder + ErrorEmbedder — test doubles for Embedder.

FakeEmbedder ports Go's internal/adapter/embedder/fake/fake.go semantics:
deterministic vectors derived from sha256(text|i), no external dependency.

Differences from Go:
  - Uses `hashlib.sha256` + `int.from_bytes` instead of crypto/sha256 +
    encoding/binary. Same algorithm, Python idioms.
  - ErrorEmbedder is a separate class (Go uses SetEmbedHook). Both
    patterns work; the separate class is more discoverable in test
    setup code ("why is this test injecting a hook?" vs "this test
    wires ErrorEmbedder").
  - ErrorEmbedder.model() delegates to a wrapped FakeEmbedder so tests
    can still assert the model slug/dimension in the error path. Mirrors
    Go's errorEmbedder.inner field.

Vector scheme: `v[i] = (int32(sha256(f"{text}|{i}")[:4]) / 2**31)`.
Same as Go — values in [-1, 1), uncorrelated across inputs, stable
across runs. NOT L2-normalized; tests that need normalized vectors
should construct them directly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from unictx.embed.embedder import ModelInfo
from unictx.embed.errors import EmbeddingFailed


def _vector_for(text: str, dimension: int) -> list[float]:
    """Deterministic vector for `text` of length `dimension`.

    Mirrors Go's Embedder.vectorFor: each component is the int32
    reinterpreted from the first 4 bytes of sha256(f"{text}|{i}"),
    scaled to [-1, 1).
    """
    v: list[float] = []
    for i in range(dimension):
        h = hashlib.sha256(f"{text}|{i}".encode()).digest()
        u = int.from_bytes(h[:4], "little", signed=False)
        # Map to [-1, 1) the same way Go does: cast u to signed int32
        # then divide by 2**31. The & 0xFFFFFFFF handles Python's
        # arbitrary-precision ints.
        signed = u if u < 2**31 else u - 2**32
        v.append(signed / float(2**31))
    return v


@dataclass(slots=True)
class FakeEmbedder:
    """Deterministic embedder for tests.

    Attributes:
      dimension: vector length. Default 1024 matches the production
        default (see config). Tests that need a different dim (e.g.,
        the search_hybrid parity test uses dim=8) pass it explicitly.
      model_info: returned by model(). When None, defaults to
        ModelInfo(slug="fake", dimension=<self.dimension>). Tests can
        inject a custom ModelInfo to exercise model-mismatch paths.
      embed_calls: list of texts lists passed to embed(), in order.
        Useful for asserting batch inputs.
    """

    dimension: int = 1024
    model_info: ModelInfo | None = None
    embed_calls: list[list[str]] = field(default_factory=list)

    def model(self) -> ModelInfo:
        if self.model_info is not None:
            return self.model_info
        return ModelInfo(slug="fake", dimension=self.dimension)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return [_vector_for(t, self.dimension) for t in texts]


@dataclass(slots=True)
class ErrorEmbedder:
    """Embedder that always raises EmbeddingFailed.

    Mirrors Go's errorEmbedder (search_hybrid_test.go). Used to prove
    IngestService/SearchService degrade gracefully on embedder outage
    instead of bubbling the error up — see Phase 5 search tests.

    Attributes:
      inner: the underlying FakeEmbedder. model() delegates here so
        tests can still assert the configured slug/dimension in the
        error path.
      reason: the failure message wrapped into EmbeddingFailed.
        Defaults to "simulated outage" (Go's wording) to ease
        cross-source review.
    """

    inner: FakeEmbedder = field(default_factory=FakeEmbedder)
    reason: str = "simulated outage"

    def model(self) -> ModelInfo:
        return self.inner.model()

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingFailed(self.inner.model().slug, self.reason)
