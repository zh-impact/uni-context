"""``search`` command — FTS-only + hybrid retrieval over the index.

Faithful port of Go's ``internal/cli/search.go``. One command::

    unictx search <query> [--mode fts-only|hybrid] [--limit N]
                          [--scope user,...] [--kind note,...]
                          [--as user|project|global] [--project ID]

Behavior preserved:

  - **Query joining:** positional args are concatenated with spaces
    (`unictx search hello world` → "hello world").
  - **Mode normalization:** ``""`` → ``"fts-only"`` (Plan 1 default).
    Non-default values validated up front against the Plan 2a allow-list
    (``fts-only | hybrid``).
  - **Scope/kind parsing:** string flags → typed lists. Invalid values
    surface a clear error echoing the bad value + the valid set.
  - **Hybrid without embedder:** SearchService degrades to fts-only
    internally (Plan 2a contract). The CLI does not need to know.
  - **Output:** ``--json`` (global flag) emits ``{results, total, mode}``;
    plain text renders ``[<id8>] <title>`` + scope/kind/score/matched_by
    + snippet. Empty result → ``(no matches)``.

Registration note: ``search`` is registered as a direct command on the
root ``app`` (``app.command(name="search")(search)`` in cli/__init__.py)
rather than as a sub-Typer with a callback. A sub-Typer's
``invoke_without_command=True`` callback uses Typer's variadic positional
parsing, which greedily consumes options that follow the positional
(``search hello --mode bogus`` parses ``--mode`` as part of the query).
Direct command registration avoids this Typer/Click quirk.
"""

from __future__ import annotations

from typing import Any

import typer

from unictx.cli.app import AppContainer, get_config_path, wire
from unictx.config import load as load_config
from unictx.items.models import Kind, Scope
from unictx.search.searcher import SearchMode
from unictx.search.service import SearchRequest

__all__ = [
    "normalize_search_mode",
    "parse_kinds",
    "parse_scopes",
    "search",
    "validate_search_mode",
]


_VALID_SCOPES = {
    "user": Scope.USER,
    "project": Scope.PROJECT,
    "global": Scope.GLOBAL,
}

# Valid access identities for --as. Maps the CLI string to the Scope
# enum. user is the default (innermost layer; sees everything).
_VALID_AS = _VALID_SCOPES

_VALID_KINDS = {
    "note": Kind.NOTE,
    "excerpt": Kind.EXCERPT,
    "link": Kind.LINK,
    "doc": Kind.DOC,
    "conversation_msg": Kind.CONVERSATION_MSG,
    "memory": Kind.MEMORY,
    "file": Kind.FILE,
}

_VALID_MODES = {"fts-only", "hybrid"}


def normalize_search_mode(mode: str) -> str:
    """Map ``""`` → ``"fts-only"`` (Plan 1 default).

    Non-default values returned verbatim; validity checked by
    :func:`validate_search_mode`.
    """
    if mode == "":
        return "fts-only"
    return mode


def validate_search_mode(mode: str) -> str | None:
    """Return error message if ``mode`` is not in the Plan 2a allow-list."""
    if mode not in _VALID_MODES:
        return f'--mode "{mode}" not supported (Plan 2a: {" | ".join(sorted(_VALID_MODES))})'
    return None


def parse_scopes(values: list[str]) -> tuple[list[Scope], str | None]:
    """Convert raw strings to typed Scopes. Returns (scopes, error_msg)."""
    out: list[Scope] = []
    for v in values:
        if v not in _VALID_SCOPES:
            return [], (f"invalid scope {v!r} (valid: user, project, global)")
        out.append(_VALID_SCOPES[v])
    return out, None


def parse_kinds(values: list[str]) -> tuple[list[Kind], str | None]:
    """Convert raw strings to typed Kinds. Returns (kinds, error_msg)."""
    out: list[Kind] = []
    for v in values:
        if v not in _VALID_KINDS:
            return [], (
                f"invalid kind {v!r} "
                "(valid: note, excerpt, link, doc, conversation_msg, memory, file)"
            )
        out.append(_VALID_KINDS[v])
    return out, None


def parse_as_scope(value: str) -> tuple[Scope, str | None]:
    """Parse the --as flag into a typed Scope. Returns (as_scope, error_msg).

    The access identity determines the visible scope set (see
    visible_scopes). Defaults to 'user' (innermost; sees everything).
    """
    if value not in _VALID_AS:
        return Scope.USER, f"invalid --as {value!r} (valid: user, project, global)"
    return _VALID_AS[value], None


def _default_load_container() -> AppContainer:
    return wire(load_config(get_config_path()))


_load_container = _default_load_container


def search(  # noqa: PLR0913 - Typer translates these to CLI flags
    query: list[str] = typer.Argument(
        None,
        help="Search query (multiple positional args concatenated with spaces).",
    ),
    scope: list[str] = typer.Option(
        [], "--scope", help="Filter by scope (user|project|global; repeatable)."
    ),
    kind: list[str] = typer.Option(
        [], "--kind", help="Filter by kind (note|doc|memory|...; repeatable)."
    ),
    limit: int = typer.Option(20, "--limit", help="Max results (default 20)."),
    mode: str = typer.Option(
        "fts-only", "--mode", help="Search mode (Plan 2a: fts-only | hybrid)."
    ),
    as_scope: str = typer.Option(
        "user",
        "--as",
        help="Access identity (user|project|global). Default user sees all; "
        "project/global see less. P1 trust boundary.",
    ),
    project: str = typer.Option(
        "",
        "--project",
        help="Project ID the caller acts as (required with --as project for "
        "project-to-project isolation).",
    ),
) -> None:
    """Search across all scopes.

    Registered as ``app.command(name="search")(search)`` in
    :mod:`unictx.cli` — see module docstring for the registration
    rationale (Typer variadic-positional quirk in callback-based sub-Typers).
    """
    from unictx.cli.app import is_json_mode

    if not query:
        typer.echo("search: query required", err=True)
        raise typer.Exit(code=2)

    query_str = " ".join(query)
    normalized = normalize_search_mode(mode)
    if err := validate_search_mode(normalized):
        typer.echo(err, err=True)
        raise typer.Exit(code=2)

    if limit <= 0:
        limit = 20

    scopes, err = parse_scopes(scope)
    if err:
        typer.echo(err, err=True)
        raise typer.Exit(code=2)
    kinds, err = parse_kinds(kind)
    if err:
        typer.echo(err, err=True)
        raise typer.Exit(code=2)
    as_scope_value, err = parse_as_scope(as_scope)
    if err:
        typer.echo(err, err=True)
        raise typer.Exit(code=2)
    # --as project requires --project for project-to-project isolation.
    # Without a project_id a PROJECT actor would have nothing to scope
    # against, and the SQL isolation predicate can't fire.
    if as_scope_value == Scope.PROJECT and not project:
        typer.echo(
            "--as project requires --project <ID> for project isolation",
            err=True,
        )
        raise typer.Exit(code=2)

    container = _load_container()
    try:
        # CLI uses hyphenated mode strings ("fts-only" / "hybrid"); the
        # SearchMode StrEnum uses underscores ("fts_only"). Map by name
        # rather than value so the hyphenated convention stays in the
        # CLI surface (user-facing) and never leaks into the enum layer.
        enum_key = normalized.upper().replace("-", "_")
        req = SearchRequest(
            query=query_str,
            scopes=scopes,
            kinds=kinds,
            limit=limit,
            mode=SearchMode[enum_key],
            as_scope=as_scope_value,
            as_project_id=project,
        )
        try:
            resp = container.search.search(req)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        if is_json_mode():
            from unictx.cli.output import print_json

            payload: dict[str, Any] = {
                "results": [
                    {
                        "id": r.item.id,
                        "title": r.item.title,
                        "scope": str(r.item.scope),
                        "kind": str(r.item.kind),
                        "score": r.score,
                        "snippet": r.snippet,
                        "matched_by": r.matched_by,
                        "tags": r.item.tags,
                        "created_at": r.item.created_at,
                    }
                    for r in resp.results
                ],
                "total": resp.total,
                "mode": normalized,
                "as_scope": str(as_scope_value),
            }
            print_json(payload)
            return

        if not resp.results:
            typer.echo("(no matches)")
            return

        for r in resp.results:
            id_prefix = r.item.id[:8]
            matched = "+".join(r.matched_by)
            typer.echo(f"[{id_prefix}]  {r.item.title}")
            typer.echo(
                f"  scope={r.item.scope} kind={r.item.kind} "
                f"score={r.score:.3f} (matched: {matched})"
            )
            typer.echo(f"  {r.snippet}")
            typer.echo("")
    finally:
        container.close()
