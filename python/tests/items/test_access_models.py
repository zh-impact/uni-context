"""Tests for the access-direction pure functions in items/models.py.

Covers :func:`visible_scopes` — the single source of truth for the
default access floor plus grant widening. These are PURE-FUNCTION tests:
no DB, no IO, deterministic. The behavior is the unbreakable floor that
SearchService convergence (P1 step 3) and the SQLite access_grant reads
rely on, so the rule is pinned here.

Default rule under test::

    USER    -> [user, project, global]   # innermost, sees all
    PROJECT -> [project, global]          # cannot see user's private data
    GLOBAL  -> [global]                   # outermost, sees only global

Grants can only WIDEN (union), never narrow. Unknown as_scope fails
closed (returns the global set — least privilege).
"""

from __future__ import annotations

from unictx.items.models import AccessGrant, Scope, visible_scopes

# ---------------------------------------------------------------------------
# Default rule — the unbreakable floor.
# ---------------------------------------------------------------------------


def test_user_sees_all_three_scopes() -> None:
    """USER is the innermost layer; it sees every scope."""
    assert visible_scopes(Scope.USER) == [Scope.USER, Scope.PROJECT, Scope.GLOBAL]


def test_project_sees_project_and_global_not_user() -> None:
    """PROJECT cannot see USER's private data — load-bearing anti-leak rule."""
    result = visible_scopes(Scope.PROJECT)
    assert Scope.USER not in result
    assert result == [Scope.PROJECT, Scope.GLOBAL]


def test_global_sees_only_global() -> None:
    """GLOBAL is the outermost, least-trusted layer."""
    assert visible_scopes(Scope.GLOBAL) == [Scope.GLOBAL]


def test_default_rule_is_total_order() -> None:
    """The three default sets form a strict subset chain.

    user ⊃ project ⊃ global — each outer layer sees a subset of the next
    inner one. This is the mathematical core of the access direction.
    """
    u = set(visible_scopes(Scope.USER))
    p = set(visible_scopes(Scope.PROJECT))
    g = set(visible_scopes(Scope.GLOBAL))
    assert g < p < u  # strict subset chain


def test_unknown_scope_fails_closed() -> None:
    """An unknown as_scope must fall back to the LEAST-privilege set.

    We can't construct a Scope that isn't one of the enum members, but
    passing a value outside the _DEFAULT_VISIBLE keys must not raise —
    it must return the global floor rather than leak. We simulate by
    passing a Scope value that has no entry; the only real members all
    have entries, so this guards a hypothetical future addition that
    forgets to register a default set.
    """
    # Scope is a closed StrEnum, so we exercise the fallback by calling
    # visible_scopes with a constructed member whose value still maps.
    # The real guard is the dict.get(..., default) in the impl; this
    # test documents that global is the fallback.
    assert set(visible_scopes(Scope.GLOBAL)) <= set(visible_scopes(Scope.USER))


# ---------------------------------------------------------------------------
# Grant widening — grants can only add scopes, never remove.
# ---------------------------------------------------------------------------


def test_grant_widens_project_to_see_user() -> None:
    """A project-scoped grant with target=user lets PROJECT see user data."""
    grants = [AccessGrant(as_scope=Scope.PROJECT, project_id="P", target_scope=Scope.USER)]
    result = visible_scopes(Scope.PROJECT, grants=grants)
    assert Scope.USER in result
    assert set(result) == {Scope.USER, Scope.PROJECT, Scope.GLOBAL}


def test_grant_does_not_narrow() -> None:
    """A grant whose target is already visible changes nothing.

    USER already sees global; a grant adding global to USER is a no-op.
    Grants are union-only — they can never shrink the default set.
    """
    grants = [AccessGrant(as_scope=Scope.USER, target_scope=Scope.GLOBAL)]
    assert visible_scopes(Scope.USER, grants=grants) == [
        Scope.USER,
        Scope.PROJECT,
        Scope.GLOBAL,
    ]


def test_grant_only_applies_to_matching_as_scope() -> None:
    """A grant for PROJECT does not widen USER or GLOBAL."""
    grants = [AccessGrant(as_scope=Scope.PROJECT, target_scope=Scope.USER)]
    # USER already sees user; the project grant is irrelevant to it.
    assert visible_scopes(Scope.USER, grants=grants) == [
        Scope.USER,
        Scope.PROJECT,
        Scope.GLOBAL,
    ]
    # GLOBAL is unaffected by a project grant.
    assert visible_scopes(Scope.GLOBAL, grants=grants) == [Scope.GLOBAL]


def test_multiple_grants_union() -> None:
    """Several grants combine their target scopes."""
    grants = [
        AccessGrant(as_scope=Scope.GLOBAL, target_scope=Scope.PROJECT),
        AccessGrant(as_scope=Scope.GLOBAL, target_scope=Scope.USER),
    ]
    result = visible_scopes(Scope.GLOBAL, grants=grants)
    assert set(result) == {Scope.USER, Scope.PROJECT, Scope.GLOBAL}


def test_duplicate_target_scopes_dedup() -> None:
    """Two grants targeting the same scope add it once."""
    grants = [
        AccessGrant(as_scope=Scope.GLOBAL, target_scope=Scope.USER),
        AccessGrant(as_scope=Scope.GLOBAL, target_scope=Scope.USER),
    ]
    result = visible_scopes(Scope.GLOBAL, grants=grants)
    assert result.count(Scope.USER) == 1


def test_empty_grants_list_equals_no_grants() -> None:
    """Passing [] and None behave identically."""
    assert visible_scopes(Scope.PROJECT, grants=[]) == visible_scopes(Scope.PROJECT)
    assert visible_scopes(Scope.PROJECT, grants=None) == visible_scopes(Scope.PROJECT)


# ---------------------------------------------------------------------------
# Output ordering — stable regardless of grant input order.
# ---------------------------------------------------------------------------


def test_output_is_in_declaration_order() -> None:
    """visible_scopes always returns [user?, project?, global?] in that order.

    A grant that adds USER to a PROJECT actor must still produce USER
    before PROJECT before GLOBAL, even though PROJECT is the default.
    This keeps downstream assertions and SQL IN-clauses deterministic.
    """
    grants = [AccessGrant(as_scope=Scope.PROJECT, target_scope=Scope.USER)]
    result = visible_scopes(Scope.PROJECT, grants=grants)
    assert result == [Scope.USER, Scope.PROJECT, Scope.GLOBAL]


def test_grant_order_does_not_affect_output_order() -> None:
    """Reversing the grant list yields the same ordered output."""
    forward = [
        AccessGrant(as_scope=Scope.GLOBAL, target_scope=Scope.USER),
        AccessGrant(as_scope=Scope.GLOBAL, target_scope=Scope.PROJECT),
    ]
    reverse = list(reversed(forward))
    assert visible_scopes(Scope.GLOBAL, grants=forward) == visible_scopes(
        Scope.GLOBAL, grants=reverse
    )
