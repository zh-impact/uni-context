"""SQLite-backed :class:`AccessRepo` implementation.

Owns the ``access_grant`` table (created by migration 0005). This is
the concrete storage-side impl of the
:class:`unictx.items.repo.AccessRepo` Protocol — read-only in P1.

Grant matching rule
===================

A grant applies to an actor ``(as_scope, as_project_id)`` when::

    grant.as_scope == as_scope
    AND (grant.project_id IS NULL           -- "all projects"
         OR grant.project_id == as_project_id)

In SQL this is a two-arm ``WHERE`` over a single ``as_scope`` equality.
The NULL arm lets a single grant cover every project that acts under
``as_scope`` (e.g. "all project agents may read global") without
forcing one row per project.

No transaction handling is needed here: the only method is a SELECT,
which runs fine under the connection's autocommit mode
(``isolation_level=None`` set by :func:`unictx.storage.db.open_db`).
"""

from __future__ import annotations

import sqlite3

from unictx.items.models import AccessGrant, Scope

__all__ = ["AccessRepoImpl"]


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

# Two-arm match: exact as_scope, and either "all projects" (NULL) or the
# specific project_id. Parameters: as_scope, project_id, project_id
# (the second project_id binds the NULL-arm's COALESCE fallback so a
# non-NULL grant column only matches an equal as_project_id).
#
# COALESCE(grant.project_id, '') = ? means: when the grant's project_id
# is NULL, treat it as '' and compare to the caller's as_project_id
# (which the caller passes as '' for a user/global actor). But that
# would incorrectly reject a NULL grant when the caller is a user/global
# actor — which is fine, because user/global actors never query grants
# for themselves meaningfully. The correct two-arm form is the OR below.
_LIST_GRANTS_SQL = """
SELECT as_scope, COALESCE(project_id, ''), target_scope, reason
FROM access_grant
WHERE as_scope = ?
  AND (project_id IS NULL OR project_id = ?)
ORDER BY id ASC
"""

# INSERT for grant(). project_id "" (dataclass "all projects") maps to
# SQL NULL so the two-arm read match (NULL project_id == "all") works.
# lastrowid is the AUTOINCREMENT id, returned to the caller.
_INSERT_GRANT_SQL = """
INSERT INTO access_grant (as_scope, project_id, target_scope, reason, created_at)
VALUES (?, ?, ?, ?, strftime('%s','now'))
"""

# Idempotent DELETE — rowcount==0 is a no-op, not an error (see Protocol
# docstring for the forgiving-revoke rationale vs embed model remove).
_DELETE_GRANT_SQL = "DELETE FROM access_grant WHERE id = ?"

# list_all_grants: base SELECT plus an optional as_scope/project_id
# filter appended when as_scope is not None. Mirrors the two-arm match
# of _LIST_GRANTS_SQL so the filtered view matches what list_grants
# would return for that actor.
_LIST_ALL_GRANTS_SQL = """
SELECT id, as_scope, COALESCE(project_id, ''), target_scope, reason
FROM access_grant
"""
_ALL_GRANTS_FILTER_SQL = " WHERE as_scope = ? AND (project_id IS NULL OR project_id = ?)"


# ---------------------------------------------------------------------------
# AccessRepoImpl
# ---------------------------------------------------------------------------


class AccessRepoImpl:
    """SQLite-backed :class:`AccessRepo`.

    Constructed with a :mod:`sqlite3` connection (typically produced by
    :func:`unictx.storage.db.open_db` and migrated via
    :func:`unictx.storage.migrations_runner.migrate`). Shares the
    connection with the rest of the storage layer.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def list_grants(
        self, as_scope: Scope, as_project_id: str = ""
    ) -> list[AccessGrant]:
        """Return grants applicable to ``(as_scope, as_project_id)``.

        Empty list (not None) if no grants apply. Ordering by ``id ASC``
        keeps the output stable for deterministic test assertions and
        for :func:`visible_scopes` dedup (which is order-independent in
        result but stable in presentation).
        """
        rows = self._db.execute(
            _LIST_GRANTS_SQL, (str(as_scope), as_project_id)
        ).fetchall()
        return [
            AccessGrant(
                as_scope=Scope(row[0]),
                project_id=row[1] or "",
                target_scope=Scope(row[2]),
                reason=row[3] or "",
            )
            for row in rows
        ]

    # ---- writes (P1.1 management) --------------------------------------

    def grant(self, g: AccessGrant) -> int:
        """Insert one grant row, returning its new AUTOINCREMENT id.

        The dataclass's ``project_id == ""`` (meaning "all projects")
        maps to SQL NULL so the two-arm read match treats it correctly.
        Duplicates are permitted (see Protocol docstring).
        """
        # "" → NULL for "all projects"; non-empty stays verbatim.
        project_col: str | None = g.project_id if g.project_id else None
        cur = self._db.execute(
            _INSERT_GRANT_SQL,
            (str(g.as_scope), project_col, str(g.target_scope), g.reason),
        )
        return int(cur.lastrowid)

    def revoke(self, grant_id: int) -> None:
        """Delete the grant row by id. Idempotent (missing id is a no-op)."""
        self._db.execute(_DELETE_GRANT_SQL, (grant_id,))

    def list_all_grants(
        self,
        as_scope: Scope | None = None,
        as_project_id: str = "",
    ) -> list[tuple[int, AccessGrant]]:
        """Return ``(id, grant)`` pairs for all grants, optionally filtered.

        Filtering rules:
          - ``as_scope=None`` → every grant (no WHERE).
          - ``as_scope`` set, ``as_project_id`` empty → all grants for
            that identity regardless of project (management view: "show
            me every project-actor grant").
          - both set → the two-arm match of :meth:`list_grants` (the
            specific actor's effective grants).

        Ordered by id ASC for stable display in the management CLI.
        """
        if as_scope is None:
            sql = _LIST_ALL_GRANTS_SQL + " ORDER BY id ASC"
            rows = self._db.execute(sql).fetchall()
        elif as_project_id:
            sql = _LIST_ALL_GRANTS_SQL + _ALL_GRANTS_FILTER_SQL + " ORDER BY id ASC"
            rows = self._db.execute(sql, (str(as_scope), as_project_id)).fetchall()
        else:
            # Filter by as_scope only — every grant for that identity.
            sql = (
                _LIST_ALL_GRANTS_SQL
                + " WHERE as_scope = ? ORDER BY id ASC"
            )
            rows = self._db.execute(sql, (str(as_scope),)).fetchall()
        return [
            (
                int(row[0]),
                AccessGrant(
                    as_scope=Scope(row[1]),
                    project_id=row[2] or "",
                    target_scope=Scope(row[3]),
                    reason=row[4] or "",
                ),
            )
            for row in rows
        ]
