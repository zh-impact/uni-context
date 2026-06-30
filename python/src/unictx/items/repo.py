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

from unictx.items.models import AccessGrant, ContextItem, Kind, Scope


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

    Access direction (P1):
      - as_scope: the caller's access identity. When set to PROJECT, the
        impl applies row-level project isolation (see as_project_id).
        Callers are responsible for converging `scopes` against the
        visible set via visible_scopes() BEFORE constructing the filter
        — the repo layer does not query grants (keeps storage decoupled
        from access policy).
      - as_project_id: when as_scope==PROJECT, restricts project-scope
        rows to those whose project_id matches. Global rows bypass this
        (shared). Empty for USER/GLOBAL actors.
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
    as_scope: Scope = Scope.USER
    as_project_id: str = ""


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


@runtime_checkable
class AccessRepo(Protocol):
    """Reads + writes access_grant rows. The P1 access-direction port.

    Read side (P1): ``list_grants`` returns the grants that apply to a
    given (as_scope, project_id) actor, which :func:`visible_scopes`
    then folds into the visible scope set.

    Write side (P1.1): ``grant`` / ``revoke`` / ``list_all_grants`` back
    the management CLI (``unictx access grant add|list|remove``).

    Grant matching rule (read): a grant applies when ``as_scope`` matches
    AND (``project_id`` matches OR the grant's ``project_id`` is empty,
    i.e. "all projects"). The implementation performs this matching in
    SQL; callers receive only the applicable grants.
    """

    def list_grants(
        self, as_scope: Scope, as_project_id: str = ""
    ) -> list[AccessGrant]:
        """Return grants applicable to the given (as_scope, project_id) actor.

        Empty list (not None) if no grants apply. Never raises on a
        missing access_grant table — the table is created by migration
        0005 and is always present after migrate().
        """
        ...

    def grant(self, g: AccessGrant) -> int:
        """Insert one grant row, returning its new AUTOINCREMENT id.

        Duplicate grants are permitted — the same authorization may be
        recorded more than once (audit-friendly, and grant semantics are
        "exists ⇒ effective", so duplicates are harmless). A unique
        constraint is a later optimization, not a P1.1 concern.
        """
        ...

    def revoke(self, grant_id: int) -> None:
        """Delete the grant row with the given id. Idempotent.

        Revoking a non-existent id is a no-op (unlike the strict
        ``embed model remove`` semantics) — grant revocation should be
        forgiving: the caller's goal ("this grant must not be in
        effect") is satisfied whether or not the row was present.
        """
        ...

    def list_all_grants(
        self,
        as_scope: Scope | None = None,
        as_project_id: str = "",
    ) -> list[tuple[int, AccessGrant]]:
        """Return ``(id, grant)`` pairs for all grants, optionally filtered.

        ``as_scope=None`` returns every grant. When set, filters to rows
        matching the two-arm rule (exact as_scope AND (NULL project OR
        exact project_id)). Ordered by id ASC for stable display.
        """
        ...
