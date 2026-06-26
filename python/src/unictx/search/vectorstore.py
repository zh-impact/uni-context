"""VectorStore Protocol + VectorQuery/VectorHit.

Ports Go's internal/port/vectorstore.go. The vec0 virtual table is
owned by this Protocol's impl (Phase 2, storage/) — vector writes live
here, not in ContextRepo or EmbeddingRepo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class VectorQuery:
    """KNN search against the vector store. Mirrors Go's port.VectorQuery.

    Filters (scopes, kinds) are pushed down to context_item via JOIN in
    the sqlite impl. Empty list = no filter on that dimension.

    Note on `vector` type: Go uses `[]float32`. We use `list[float]` —
    concrete and matches Go's concreteness. `Sequence[float]` was
    considered but rejected because it would accept tuples/iterators
    that the impl likely needs to index/slice, and we want callers to
    pass a real list.
    """

    vector: list[float] = field(default_factory=list)
    model: str = ""  # slug, must match embedding_model.slug
    limit: int = 0
    scopes: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)


@dataclass(slots=True)
class VectorHit:
    """One KNN result. Mirrors Go's port.VectorHit.

    score is caller-normalized from distance — higher = better.
    distance is the raw vec0 distance — lower = better. Both are
    returned because different callers want different signals.
    """

    id: str = ""
    score: float = 0.0
    distance: float = 0.0


@runtime_checkable
class VectorStore(Protocol):
    """Reads and writes embeddings keyed by item_id. Mirrors Go's port.VectorStore.

    A given (model, item_id) has at most one embedding — PRIMARY KEY in
    context_embedding. `put` is idempotent (insert-or-replace). `delete`
    is no-op if absent.
    """

    def put(self, model: str, item_id: str, vector: list[float]) -> None: ...

    def search(self, q: VectorQuery) -> list[VectorHit]:
        """KNN query. Returns hits sorted by score DESC."""
        ...

    def delete(self, model: str, item_id: str) -> None: ...
