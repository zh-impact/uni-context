"""AccessService — application-layer boundary for access-grant management.

Thin pass-through over :class:`AccessRepo` — the value is the boundary,
not added logic. Routing the ``access grant add/list/remove`` CLI
commands through this service means the CLI has no direct dependency on
the storage impl, so the grant table's backing store can change (e.g.
swap SQLite for another metadata store) without touching the inbound
layer. Mirrors the ModelService pattern (``embed/model_service.py``):
both exist to keep ``cli/*`` free of ``storage/*_impl`` imports (enforced
by ``tests/cli/test_no_direct_storage_import.py``).

Lives in ``items/`` because access direction is an items-domain concern
(:func:`visible_scopes` and :class:`AccessGrant` already live in
``items/models.py``); it is the same layer as :class:`ItemService`.

Plan §Python Conventions adaptations:
  - ctx dropped (Python is sync).
  - Go-style tuple returns ``(T, error)`` → Python returns ``T`` or raises.
"""

from __future__ import annotations

from unictx.items.models import AccessGrant, Scope
from unictx.items.repo import AccessRepo

__all__ = ["AccessService"]


class AccessService:
    """Application-layer boundary for access-grant lifecycle.

    Constructed with an :class:`AccessRepo`. The service is always
    constructed (unlike embed-pipeline services that are gated on
    ``embedder.enabled``) — access grants are relevant in every plan.
    """

    def __init__(self, repo: AccessRepo) -> None:
        self._repo = repo

    def grant(self, g: AccessGrant) -> int:
        """Insert one grant, returning its new id. Forwards verbatim.

        Duplicate-grant handling, NULL project_id mapping, and id
        assignment are the repo's responsibility.
        """
        return self._repo.grant(g)

    def revoke(self, grant_id: int) -> None:
        """Delete the grant row by id. Idempotent (missing id is a no-op)."""
        self._repo.revoke(grant_id)

    def list_grants(
        self, as_scope: Scope, as_project_id: str = ""
    ) -> list[AccessGrant]:
        """Effective grants for a specific actor (the read-side convergence input)."""
        return self._repo.list_grants(as_scope, as_project_id)

    def list_all_grants(
        self,
        as_scope: Scope | None = None,
        as_project_id: str = "",
    ) -> list[tuple[int, AccessGrant]]:
        """All grants with ids (management view), optionally filtered."""
        return self._repo.list_all_grants(as_scope, as_project_id)
