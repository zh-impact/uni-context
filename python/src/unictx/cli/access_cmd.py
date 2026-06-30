"""``access`` command family — manage access-direction grants.

Backs the P1.1 grant-management CLI. Three commands across a nested
Typer group (mirrors ``embed model add|list|remove``)::

    access grant add --as project|global [--project ID] \
                     --target user|project|global [--reason "..."]
    access grant list [--as project|global] [--project ID]
    access grant remove <ID>

Behavior:

  - **--as validation:** ``--as user`` is rejected — USER is the
    innermost layer and sees everything by default, so a user grant is
    meaningless. Only ``project`` / ``global`` may be granted.
  - **--project optionality:** unlike ``search --as project`` (which
    requires --project to know WHICH project is querying), here --project
    is OPTIONAL: omitting it means "all projects acting as this identity"
    (DB NULL). This is authorization, not a query context.
  - **remove is idempotent:** revoking a non-existent id is a no-op
    (exit 0), matching the forgiving-revoke contract in AccessRepo.
  - **Output:** tab-aligned tables for ``grant list`` (Go uses
    text/tabwriter; we pad with two spaces); one-line confirmations for
    add/remove; ``--json`` emits machine-readable payloads.

Container injection: ``_load_container`` is a module-level seam
(defaulting to ``wire(load_config(get_config_path()))``). Tests
monkeypatch it to inject a stubbed container — the same pattern
embed_cmd.py and user_note.py use.
"""

from __future__ import annotations

import typer

from unictx.cli.app import AppContainer, get_config_path, wire
from unictx.config import load as load_config
from unictx.items.models import AccessGrant, Scope

__all__ = [
    "access_app",
    "format_grant_row",
    "grant_add",
    "grant_list",
    "grant_remove",
    "validate_grant_add_args",
]


# ---------------------------------------------------------------------------
# Container seam
# ---------------------------------------------------------------------------


def _default_load_container() -> AppContainer:
    return wire(load_config(get_config_path()))


_load_container = _default_load_container


# ---------------------------------------------------------------------------
# Typer groups
# ---------------------------------------------------------------------------


access_app = typer.Typer(
    name="access",
    help="Manage access-direction grants (authorize project/global actors).",
    no_args_is_help=True,
    add_completion=False,
)

grant_app = typer.Typer(
    name="grant",
    help="Add/list/remove access grants.",
    no_args_is_help=True,
    add_completion=False,
)

access_app.add_typer(grant_app, name="grant")


# ---------------------------------------------------------------------------
# Helpers (pure — unit-tested directly)
# ---------------------------------------------------------------------------

# Identities that may be granted. USER is excluded — it's the innermost
# layer and sees everything by default, so granting it is meaningless.
_GRANTABLE_AS = {"project": Scope.PROJECT, "global": Scope.GLOBAL}
_VALID_TARGETS = {"user": Scope.USER, "project": Scope.PROJECT, "global": Scope.GLOBAL}

_GRANT_HEADER = "\t".join(["ID", "AS_SCOPE", "PROJECT_ID", "TARGET_SCOPE", "REASON"])


def validate_grant_add_args(as_scope: str, target: str) -> str | None:
    """Return error message if ``access grant add`` inputs are invalid.

    ``--as user`` is rejected (meaningless). ``--as`` / ``--target`` must
    name valid scopes. ``--project`` is validated by the caller only for
    emptiness-vs-presence (it's legitimately optional here).
    """
    if as_scope == "user":
        return (
            "access grant add: --as user is not grantable "
            "(user sees everything by default)"
        )
    if as_scope not in _GRANTABLE_AS:
        return (
            f"access grant add: invalid --as {as_scope!r} "
            "(grantable: project, global)"
        )
    if target not in _VALID_TARGETS:
        return (
            f"access grant add: invalid --target {target!r} "
            "(valid: user, project, global)"
        )
    return None


def format_grant_row(gid: int, g: AccessGrant) -> str:
    """Render one grant as a tab-separated table row.

    Columns: ID, AS_SCOPE, PROJECT_ID (``*`` for "all projects"), TARGET_SCOPE,
    REASON. Padded with tabs (two-space look via the table header).
    """
    project_cell = g.project_id if g.project_id else "*"
    return "\t".join(
        [str(gid), str(g.as_scope), project_cell, str(g.target_scope), g.reason]
    )


# ---------------------------------------------------------------------------
# access grant add
# ---------------------------------------------------------------------------


@grant_app.command("add")
def grant_add(
    as_scope: str = typer.Option(
        ...,
        "--as",
        help="Identity to grant (project|global; user is not grantable).",
    ),
    project: str = typer.Option(
        "",
        "--project",
        help="Restrict to one project ID; omit for 'all projects'.",
    ),
    target: str = typer.Option(
        ...,
        "--target",
        help="Scope whose data the actor may now see (user|project|global).",
    ),
    reason: str = typer.Option(
        "", "--reason", help="Human-readable justification (audit trail)."
    ),
) -> None:
    """Grant an access-direction authorization (inserts a grant row)."""
    if err := validate_grant_add_args(as_scope, target):
        typer.echo(err, err=True)
        raise typer.Exit(code=2)

    container = _load_container()
    try:
        g = AccessGrant(
            as_scope=_GRANTABLE_AS[as_scope],
            project_id=project,
            target_scope=_VALID_TARGETS[target],
            reason=reason,
        )
        try:
            gid = container.access_svc.grant(g)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        from unictx.cli.app import is_json_mode

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json(
                {
                    "id": gid,
                    "as_scope": str(g.as_scope),
                    "project_id": g.project_id,
                    "target_scope": str(g.target_scope),
                    "reason": g.reason,
                    "status": "granted",
                }
            )
        else:
            project_part = f" project {project}" if project else " all projects"
            typer.echo(
                f"granted: {as_scope}{project_part} may access {target} (id={gid})"
            )
    finally:
        container.close()


# ---------------------------------------------------------------------------
# access grant list
# ---------------------------------------------------------------------------


@grant_app.command("list")
def grant_list(
    as_scope: str = typer.Option(
        "", "--as", help="Filter by identity (project|global); omit for all."
    ),
    project: str = typer.Option(
        "", "--project", help="Filter by project ID (only with --as)."
    ),
) -> None:
    """List access grants (optionally filtered by identity/project)."""
    container = _load_container()
    try:
        # Normalize the optional filter to a typed Scope or None.
        scope_filter: Scope | None
        if as_scope == "":
            scope_filter = None
        elif as_scope in _GRANTABLE_AS:
            scope_filter = _GRANTABLE_AS[as_scope]
        elif as_scope == "user":
            typer.echo(
                "access grant list: --as user is not grantable (no user grants exist)",
                err=True,
            )
            raise typer.Exit(code=2)
        else:
            typer.echo(
                f"access grant list: invalid --as {as_scope!r} "
                "(grantable: project, global)",
                err=True,
            )
            raise typer.Exit(code=2)

        try:
            grants = container.access_svc.list_all_grants(
                as_scope=scope_filter, as_project_id=project
            )
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        from unictx.cli.app import is_json_mode

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json(
                {
                    "grants": [
                        {
                            "id": gid,
                            "as_scope": str(g.as_scope),
                            "project_id": g.project_id,
                            "target_scope": str(g.target_scope),
                            "reason": g.reason,
                        }
                        for gid, g in grants
                    ]
                }
            )
            return

        if not grants:
            typer.echo("(no grants)")
            return

        typer.echo(_GRANT_HEADER)
        for gid, g in grants:
            typer.echo(format_grant_row(gid, g))
    finally:
        container.close()


# ---------------------------------------------------------------------------
# access grant remove
# ---------------------------------------------------------------------------


@grant_app.command("remove")
def grant_remove(
    grant_id: int = typer.Argument(..., help="Grant id to revoke (see `access grant list`)."),
) -> None:
    """Revoke a grant by id (idempotent — missing id is a no-op)."""
    container = _load_container()
    try:
        try:
            container.access_svc.revoke(grant_id)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        from unictx.cli.app import is_json_mode

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json({"id": grant_id, "status": "revoked"})
        else:
            typer.echo(f"revoked: {grant_id}")
    finally:
        container.close()
