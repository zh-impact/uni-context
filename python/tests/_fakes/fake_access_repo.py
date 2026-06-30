"""FakeAccessRepo — in-memory AccessRepo for service-layer tests.

Mirrors the FakeContextRepo pattern: a list-backed stub that service
tests can seed directly. Unlike the SQLite impl, this fake performs the
grant matching in pure Python so tests don't need a migrated DB to
exercise the SearchService convergence logic.

Tests seed ``.grants`` with :class:`AccessGrant` instances; the fake
filters them by (as_scope, project_id) on each call. Write methods
(``grant`` / ``revoke``) mutate the same list so service/CLI tests can
exercise the management flow without a DB.

To preserve the SQLite impl's id semantics (AUTOINCREMENT returned by
``grant`` and consumed by ``revoke``), the fake tracks ids in a parallel
dict. ``next_id`` seeds the counter so tests don't collide with real ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from unictx.items.models import AccessGrant, Scope
from unictx.items.repo import AccessRepo


@dataclass(slots=True)
class FakeAccessRepo:
    """List-backed AccessRepo. Tests mutate ``.grants`` directly to seed.

    Attributes:
        grants: the full grant list. list_grants filters this on each
          call by (as_scope, project_id), reproducing the SQLite impl's
          two-arm match (NULL/empty project_id matches all).
        _ids: maps grant id -> the AccessGrant in ``grants`` (parallel
          structure so revoke(id) can locate a grant by id without
          scanning). Initialized lazily by grant(); tests that seed
          ``.grants`` directly won't have ids until grant() runs.
        next_id: counter for grant() to hand out ids. Starts at 1 to
          match SQLite AUTOINCREMENT (first row id = 1).
    """

    grants: list[AccessGrant] = field(default_factory=list)
    _ids: dict[int, AccessGrant] = field(default_factory=dict)
    next_id: int = 1

    def list_grants(
        self, as_scope: Scope, as_project_id: str = ""
    ) -> list[AccessGrant]:
        out: list[AccessGrant] = []
        for grant in self.grants:
            if grant.as_scope != as_scope:
                continue
            # Empty grant.project_id == "all projects" (the DB stores
            # this as NULL; the dataclass normalizes to "").
            if grant.project_id and grant.project_id != as_project_id:
                continue
            out.append(grant)
        return out

    def grant(self, g: AccessGrant) -> int:
        gid = self.next_id
        self.next_id += 1
        self.grants.append(g)
        self._ids[gid] = g
        return gid

    def revoke(self, grant_id: int) -> None:
        g = self._ids.pop(grant_id, None)
        if g is not None:
            # Remove by identity (same object), preserving the other
            # grants. A grant seeded directly into .grants (not via
            # grant()) has no _ids entry → no-op, matching the SQLite
            # impl's idempotent DELETE.
            self.grants = [x for x in self.grants if x is not g]

    def list_all_grants(
        self,
        as_scope: Scope | None = None,
        as_project_id: str = "",
    ) -> list[tuple[int, AccessGrant]]:
        out: list[tuple[int, AccessGrant]] = []
        for gid, grant in self._ids.items():
            if as_scope is not None:
                if grant.as_scope != as_scope:
                    continue
                # Two-arm match only when a specific actor is in context;
                # empty as_project_id means "show all grants for this
                # identity" (management view), mirroring the SQLite impl.
                if as_project_id and grant.project_id and grant.project_id != as_project_id:
                    continue
            out.append((gid, grant))
        out.sort(key=lambda pair: pair[0])
        return out


# Make the fake satisfy the runtime_checkable AccessRepo Protocol.
assert isinstance(FakeAccessRepo(), AccessRepo), (
    "FakeAccessRepo must satisfy the AccessRepo Protocol"
)
