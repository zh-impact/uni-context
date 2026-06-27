"""ItemService — query-side use case for context items.

Behavior-port of Go's ``internal/service/item.go``. Owns the
externalization-hydration policy (Content inline vs ContentURI →
FileStore) so inbound adapters (CLI) read items through a service
instead of reaching into Repo + FileStore ports directly.

Plan §Python Conventions adaptations:
  - ctx dropped (Python is sync).
  - Go's ``Get(ctx, id) (ContextItem, error)`` → ``get(id) -> ContextItem``;
    missing-item raises ``ItemNotFound`` directly (no wrap).
  - List returns the same tuple shape as ``ContextRepo.list``:
    ``(rows, next_cursor)``.

The hydration logic mirrors ``EmbedService._hydrate_content`` (the
two grew up side-by-side in Go too); consolidating them onto a shared
helper is a worthwhile follow-up but out of scope here.
"""

from __future__ import annotations

from unictx.items.models import ContextItem
from unictx.items.repo import ContextRepo, ItemFilter
from unictx.storage.filestore import FileStore

__all__ = ["ItemService"]


class ItemService:
    """Query-side service for ``ContextItem``. Stateless aside from deps.

    Constructed with a ``ContextRepo`` (get/list/delete) + a
    ``FileStore`` (used only by ``get`` to hydrate externalized content).
    """

    def __init__(self, repo: ContextRepo, fs: FileStore) -> None:
        self._repo = repo
        self._fs = fs

    def get(self, item_id: str) -> ContextItem:
        """Return a fully-hydrated item.

        Policy:
          - Inline content (``item.content`` set) → returned as-is.
          - Externalized (``item.content == ""`` AND ``content_uri != ""``)
            → bytes loaded from FileStore and decoded into ``item.content``.
          - Title-only (no content, no URI) → returned with content="".

        Raises:
            ItemNotFound: propagated from repo.get (no wrap) so callers
                can distinguish missing-item from hydration failures.
            ExternalizedContentMissing: wrapped with the URI when fs.get
                fails, so dangling pointers are diagnosable.
        """
        item = self._repo.get(item_id)
        if item.content:
            return item
        if item.content_uri == "":
            return item  # title-only; nothing to hydrate
        # Hydrate externalized content. Decode UTF-8 with replace so
        # malformed bytes in PDFs don't crash the read path — matches
        # EmbedService._hydrate_content's decode policy.
        raw = self._fs.get(item.content_uri)
        item.content = raw.decode("utf-8", errors="replace")
        return item

    def list(
        self, filter: ItemFilter
    ) -> tuple[list[ContextItem], str]:
        """Delegate to repo.list with the caller's filter (scope/kind/tags/owner).

        Pagination cursor passes through unchanged. Content hydration
        is NOT applied to listed items — list returns the raw repo rows
        (externalized items come back with content=""). Callers needing
        hydrated content on a list result should call get(id) per item;
        in practice the list view shows title/summary which are inline.
        """
        return self._repo.list(filter)

    def delete(self, item_id: str) -> None:
        """Delegate to repo.delete. No FileStore cleanup (refcount-managed)."""
        self._repo.delete(item_id)
