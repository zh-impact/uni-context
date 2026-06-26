"""ContextRepo Protocol + ItemFilter — persistence port for ContextItem.

Ports Go's internal/port/repository.go (ContextRepo + ItemFilter only).
ProjectRepo and SchemaMeta from the same Go file are deferred to a later
task — the storage/ impl in Phase 2 will own them.

Adaptations from Go (per Plan §Python Conventions):
  - ctx context.Context params dropped (sync Python has no ctx).
  - Snake_case method names.
  - ItemFilter is `@dataclass(slots=True)` per Plan §Python Conventions.
  - AnyEmbedding uses `int | None` instead of Go's `*int` for the
    tri-state "no filter / only-unembedded / only-embedded" semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from unictx.items.models import ContextItem, Kind, Scope


@dataclass(slots=True)
class ItemFilter:
    """Narrows a ContextRepo.list query. Mirrors Go's port.ItemFilter.

    Field order matches Go struct declaration for cross-source review.

    Notes on semantics:
      - tags: OR semantics in Go (item matches if it has any of these
        tags). Plan §1 called out AND-vs-OR as an open issue; the impl
        in Phase 2 will pin the semantics. Field ported as-is.
      - any_embedding: tri-state via `int | None`. None = no filter,
        0 = only items NOT yet embedded, 1 = only items already embedded.
        Matches Go's `*int` zero-value-means-no-filter pattern.
      - not_done_for_model: when non-empty, restricts results to items
        lacking a status='done' row in context_embedding for this
        model_slug. Used by ReembedService for migration tracking.
    """

    scopes: list[Scope] = field(default_factory=list)
    kinds: list[Kind] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    owner_user_id: str = ""
    project_id: str = ""
    cursor: str = ""  # opaque; created_at + id encoded by impl
    limit: int = 0
    any_embedding: int | None = None
    not_done_for_model: str = ""


@runtime_checkable
class ContextRepo(Protocol):
    """Persistence port for ContextItem. Mirrors Go's port.ContextRepo.

    Implementations live in storage/ (Phase 2). The Protocol lives here
    in items/ because items/ owns the ContextItem domain type —
    storage/ imports from here, never the reverse.

    Cursor-based pagination: `list` returns (rows, next_cursor). Pass
    next_cursor back via ItemFilter.cursor to fetch the next page.
    `next_cursor(item)` builds the cursor from a single item — callers
    use the last item of the page.
    """

    def create(self, item: ContextItem) -> None: ...

    def get(self, id: str) -> ContextItem:
        """Return the item, or raise ItemNotFound if no row matches."""
        ...

    def update(self, item: ContextItem) -> ContextItem:
        """Update an item, returning the updated row."""
        ...

    def delete(self, id: str) -> None: ...

    def list(self, filter: ItemFilter) -> tuple[list[ContextItem], str]:
        """Return (rows, next_cursor). next_cursor is "" if no more rows."""
        ...

    def next_cursor(self, item: ContextItem) -> str: ...

    def reindex_fts(self, id: str, title: str, summary: str, content: str) -> None:
        """Rewrite the FTS row for the given item. Idempotent.

        Used by IngestService when content was externalized — the AFTER
        INSERT trigger captured empty content, making the item
        unsearchable. For inline items this is a harmless overwrite.
        """
        ...
