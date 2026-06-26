"""EmbeddingRepo Protocol + EmbeddingStatus.

Ports Go's internal/port/embeddingrepo.go. This Protocol owns the
context_embedding table — STATUS ONLY, no vector methods. Vector
writes live in search/vectorstore.py:VectorStore. The split mirrors
Go's split between EmbeddingRepo and VectorStore.

Rationale (from Go doc): context_embedding serves a different consumer
(worker + observability) than the vector index does (search), and
mixing them produces a fat interface. Status rows are read by the
`embed status <id>` CLI and the reembed worker; vector rows are read
by Searcher.search_vector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class EmbeddingStatus:
    """One row in context_embedding. Mirrors Go's port.EmbeddingStatus.

    Mirrors migrations 0002 + 0003. error is the original error text
    (0002 column); last_error is the most recent error text (0003
    column, added after 0002 shipped). Both retained for forward-compat
    with existing rows.
    """

    item_id: str = ""
    model_slug: str = ""
    status: str = ""  # "done" | "failed"
    error: str = ""  # original error text (0002 column)
    last_error: str = ""  # most recent error text (0003 column)
    attempts: int = 0
    embedded_at: int = 0  # unix timestamp; Go uses time.Time, we use int


@runtime_checkable
class EmbeddingRepo(Protocol):
    """Owns the context_embedding table. Mirrors Go's port.EmbeddingRepo.

    Status-only: no vector methods. Vector writes go through VectorStore.

    `get_status` raises StatusNotFound when no row matches. `list_failed`
    and `list_for_item` return empty lists (not None) when no rows match
    — callers depend on `len(rows) == 0` without None-checking.
    """

    def upsert_status(self, item_id: str, model_slug: str, status: str, err_str: str) -> None:
        """Insert or update the status row for (item_id, model_slug).

        On conflict: attempts is incremented by 1 (fresh INSERT starts
        at 1), embedded_at is set to now, status/error/last_error are
        overwritten.
        """
        ...

    def get_status(self, item_id: str, model_slug: str) -> EmbeddingStatus:
        """Return the row for (item_id, model_slug). Raise StatusNotFound if absent."""
        ...

    def list_failed(self, limit: int) -> list[EmbeddingStatus]:
        """Up to `limit` rows with status='failed', ordered embedded_at ASC.

        Oldest failures first — they've waited longest. limit<=0
        defaults to 100.
        """
        ...

    def list_for_item(self, item_id: str) -> list[EmbeddingStatus]:
        """All status rows for the item, ordered by model_slug ASC.

        Empty list (not None) if no rows. Used by the
        `embed status <id>` CLI to show per-model migration state.
        """
        ...
