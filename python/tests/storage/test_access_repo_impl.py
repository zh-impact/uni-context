"""Tests for unictx.storage.access_repo_impl.AccessRepoImpl.

Covers the SQLite-backed read of the ``access_grant`` table (migration
0005). The fixture ``migrated_db`` yields a fresh ``:memory:`` DB with
all migrations applied, so the table exists without further setup.

The grant matching rule under test (the two-arm WHERE)::

    grant.as_scope == as_scope
    AND (grant.project_id IS NULL OR grant.project_id == as_project_id)

Behavioral coverage:
* empty table → []  (never None)
* exact as_scope + NULL project_id → matches any project actor
* exact as_scope + non-NULL project_id → matches only that project
* mismatched as_scope → no match
* Protocol conformance (isinstance(AccessRepoImpl(db), AccessRepo))
* migration 0005 actually created the table (schema_version bumped to 5)
"""

from __future__ import annotations

import sqlite3

import pytest

from unictx.items.models import AccessGrant, Scope
from unictx.items.repo import AccessRepo
from unictx.storage.access_repo_impl import AccessRepoImpl

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def access(migrated_db: sqlite3.Connection) -> AccessRepoImpl:
    """AccessRepoImpl wired to a migrated :memory: DB (empty access_grant)."""
    return AccessRepoImpl(migrated_db)


def _insert_grant(
    db: sqlite3.Connection,
    *,
    as_scope: Scope,
    project_id: str | None,
    target_scope: Scope,
    reason: str = "",
) -> None:
    """Raw INSERT into access_grant for fixture seeding.

    project_id=None maps to SQL NULL ("all projects"). A non-empty
    string maps to a specific project.
    """
    db.execute(
        """
        INSERT INTO access_grant (as_scope, project_id, target_scope, reason, created_at)
        VALUES (?, ?, ?, ?, strftime('%s','now'))
        """,
        (str(as_scope), project_id, str(target_scope), reason),
    )


# ---------------------------------------------------------------------------
# Protocol + schema
# ---------------------------------------------------------------------------


def test_impl_satisfies_access_repo_protocol(access: AccessRepoImpl) -> None:
    assert isinstance(access, AccessRepo)


def test_migration_0005_created_access_grant_table(migrated_db: sqlite3.Connection) -> None:
    """The access_grant table exists and schema_version was bumped to 5."""
    (version,) = migrated_db.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    assert version == "5"

    # Table presence check via sqlite_master.
    (name,) = migrated_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='access_grant'"
    ).fetchone()
    assert name == "access_grant"


# ---------------------------------------------------------------------------
# Empty / base cases
# ---------------------------------------------------------------------------


def test_empty_table_returns_empty_list_not_none(
    access: AccessRepoImpl, migrated_db: sqlite3.Connection
) -> None:
    """No grants inserted → list_grants returns [] (never None)."""
    assert access.list_grants(Scope.PROJECT, "P") == []
    assert access.list_grants(Scope.USER) == []


# ---------------------------------------------------------------------------
# Grant matching rule
# ---------------------------------------------------------------------------


def test_null_project_id_matches_all_projects(
    access: AccessRepoImpl, migrated_db: sqlite3.Connection
) -> None:
    """A grant with project_id NULL applies to every project actor."""
    _insert_grant(
        migrated_db,
        as_scope=Scope.PROJECT,
        project_id=None,
        target_scope=Scope.USER,
        reason="all project agents may read user data",
    )
    # Project P, Q, and any other all see this grant.
    grants_p = access.list_grants(Scope.PROJECT, "P")
    grants_q = access.list_grants(Scope.PROJECT, "Q")
    assert [g.target_scope for g in grants_p] == [Scope.USER]
    assert [g.target_scope for g in grants_q] == [Scope.USER]


def test_specific_project_id_matches_only_that_project(
    access: AccessRepoImpl, migrated_db: sqlite3.Connection
) -> None:
    """A grant with a non-NULL project_id matches only that exact project."""
    _insert_grant(
        migrated_db,
        as_scope=Scope.PROJECT,
        project_id="P",
        target_scope=Scope.GLOBAL,
    )
    assert [g.target_scope for g in access.list_grants(Scope.PROJECT, "P")] == [
        Scope.GLOBAL
    ]
    # Project Q does not see P's grant.
    assert access.list_grants(Scope.PROJECT, "Q") == []


def test_mismatched_as_scope_returns_empty(
    access: AccessRepoImpl, migrated_db: sqlite3.Connection
) -> None:
    """A project grant does not surface for a global actor."""
    _insert_grant(
        migrated_db,
        as_scope=Scope.PROJECT,
        project_id=None,
        target_scope=Scope.USER,
    )
    assert access.list_grants(Scope.GLOBAL) == []
    assert access.list_grants(Scope.GLOBAL, "P") == []


def test_null_and_specific_grants_combine(
    access: AccessRepoImpl, migrated_db: sqlite3.Connection
) -> None:
    """A project actor sees both the all-projects grant and its own."""
    _insert_grant(migrated_db, as_scope=Scope.PROJECT, project_id=None, target_scope=Scope.USER)
    _insert_grant(migrated_db, as_scope=Scope.PROJECT, project_id="P", target_scope=Scope.GLOBAL)
    # Project P sees both.
    p_scopes = {g.target_scope for g in access.list_grants(Scope.PROJECT, "P")}
    assert p_scopes == {Scope.USER, Scope.GLOBAL}
    # Project Q sees only the all-projects grant.
    q_scopes = {g.target_scope for g in access.list_grants(Scope.PROJECT, "Q")}
    assert q_scopes == {Scope.USER}


# ---------------------------------------------------------------------------
# Row → dataclass mapping
# ---------------------------------------------------------------------------


def test_returned_grant_fields_round_trip(
    access: AccessRepoImpl, migrated_db: sqlite3.Connection
) -> None:
    """All AccessGrant fields map correctly from the row."""
    _insert_grant(
        migrated_db,
        as_scope=Scope.PROJECT,
        project_id="P",
        target_scope=Scope.USER,
        reason="audited access",
    )
    grants = access.list_grants(Scope.PROJECT, "P")
    assert len(grants) == 1
    g = grants[0]
    assert g.as_scope == Scope.PROJECT
    assert g.project_id == "P"
    assert g.target_scope == Scope.USER


def test_null_project_id_normalizes_to_empty_string(
    access: AccessRepoImpl, migrated_db: sqlite3.Connection
) -> None:
    """SQL NULL project_id surfaces as "" on the dataclass (domain convention)."""
    _insert_grant(
        migrated_db,
        as_scope=Scope.PROJECT,
        project_id=None,
        target_scope=Scope.USER,
    )
    g = access.list_grants(Scope.PROJECT, "P")[0]
    assert g.project_id == ""


def test_grants_ordered_by_id_asc(
    access: AccessRepoImpl, migrated_db: sqlite3.Connection
) -> None:
    """list_grants returns grants in insertion (id ASC) order — deterministic."""
    _insert_grant(migrated_db, as_scope=Scope.GLOBAL, project_id=None, target_scope=Scope.PROJECT)
    _insert_grant(migrated_db, as_scope=Scope.GLOBAL, project_id=None, target_scope=Scope.USER)
    grants = access.list_grants(Scope.GLOBAL)
    # PROJECT was inserted first (lower id), so it comes first.
    assert [g.target_scope for g in grants] == [Scope.PROJECT, Scope.USER]


# ===========================================================================
# P1.1: write methods — grant / revoke / list_all_grants.
# ===========================================================================


def test_grant_inserts_row_and_returns_id(
    access: AccessRepoImpl, migrated_db: sqlite3.Connection
) -> None:
    """grant() INSERTs and returns the AUTOINCREMENT id."""
    gid = access.grant(
        AccessGrant(as_scope=Scope.PROJECT, project_id="P", target_scope=Scope.USER)
    )
    assert gid > 0
    # The row is visible to list_grants for the matching actor.
    grants = access.list_grants(Scope.PROJECT, "P")
    assert [g.target_scope for g in grants] == [Scope.USER]


def test_grant_all_projects_stores_null_project_id(
    access: AccessRepoImpl, migrated_db: sqlite3.Connection
) -> None:
    """grant() with empty project_id maps to SQL NULL (matches all projects)."""
    access.grant(
        AccessGrant(as_scope=Scope.PROJECT, project_id="", target_scope=Scope.USER)
    )
    # NULL project_id → matches ANY project actor (the "all" arm).
    assert [g.target_scope for g in access.list_grants(Scope.PROJECT, "P")] == [
        Scope.USER
    ]
    assert [g.target_scope for g in access.list_grants(Scope.PROJECT, "Q")] == [
        Scope.USER
    ]


def test_grant_preserves_reason(
    access: AccessRepoImpl, migrated_db: sqlite3.Connection
) -> None:
    """grant() stores the reason verbatim for audit."""
    access.grant(
        AccessGrant(
            as_scope=Scope.PROJECT,
            project_id="P",
            target_scope=Scope.USER,
            reason="audited cross-team access",
        )
    )
    (reason,) = migrated_db.execute(
        "SELECT reason FROM access_grant WHERE as_scope='project'"
    ).fetchone()
    assert reason == "audited cross-team access"


def test_grant_allows_duplicates(access: AccessRepoImpl) -> None:
    """The same grant can be inserted twice (duplicates are harmless)."""
    g = AccessGrant(as_scope=Scope.PROJECT, project_id="P", target_scope=Scope.USER)
    id1 = access.grant(g)
    id2 = access.grant(g)
    assert id1 != id2  # distinct ids
    # Both rows present.
    assert len(access.list_grants(Scope.PROJECT, "P")) == 2


def test_revoke_deletes_by_id(access: AccessRepoImpl) -> None:
    """revoke(id) removes exactly that row."""
    gid = access.grant(
        AccessGrant(as_scope=Scope.PROJECT, project_id="P", target_scope=Scope.USER)
    )
    assert access.list_grants(Scope.PROJECT, "P")
    access.revoke(gid)
    assert access.list_grants(Scope.PROJECT, "P") == []


def test_revoke_missing_id_is_noop(access: AccessRepoImpl) -> None:
    """revoke of a non-existent id does not raise (idempotent, forgiving)."""
    # No grant with id 9999 exists; revoke must not raise.
    access.revoke(9999)


def test_list_all_grants_unfiltered(access: AccessRepoImpl) -> None:
    """list_all_grants() with as_scope=None returns every grant with its id."""
    id1 = access.grant(
        AccessGrant(as_scope=Scope.PROJECT, project_id="P", target_scope=Scope.USER)
    )
    id2 = access.grant(
        AccessGrant(as_scope=Scope.GLOBAL, project_id="", target_scope=Scope.PROJECT)
    )
    all_grants = access.list_all_grants()
    assert {gid for gid, _ in all_grants} == {id1, id2}
    # Ordered by id ASC.
    assert [gid for gid, _ in all_grants] == sorted([id1, id2])


def test_list_all_grants_filtered_by_as_scope(access: AccessRepoImpl) -> None:
    """list_all_grants(as_scope=X) returns only grants for that identity."""
    access.grant(
        AccessGrant(as_scope=Scope.PROJECT, project_id="P", target_scope=Scope.USER)
    )
    access.grant(
        AccessGrant(as_scope=Scope.GLOBAL, project_id="", target_scope=Scope.PROJECT)
    )
    project_grants = access.list_all_grants(as_scope=Scope.PROJECT)
    assert len(project_grants) == 1
    assert project_grants[0][1].as_scope == Scope.PROJECT


def test_list_all_grants_filtered_by_project_id(access: AccessRepoImpl) -> None:
    """The project_id filter narrows to the matching project + all-projects grants."""
    access.grant(
        AccessGrant(as_scope=Scope.PROJECT, project_id="P", target_scope=Scope.USER)
    )
    access.grant(
        AccessGrant(as_scope=Scope.PROJECT, project_id="", target_scope=Scope.GLOBAL)
    )
    # Filtered to project P: its own row + the all-projects (NULL) row.
    grants_p = access.list_all_grants(as_scope=Scope.PROJECT, as_project_id="P")
    assert {g.target_scope for _, g in grants_p} == {Scope.USER, Scope.GLOBAL}
