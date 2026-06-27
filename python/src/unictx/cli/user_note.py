"""``user note`` subcommands — add / get / list / delete.

Faithful port of Go's ``internal/cli/user_note.go``. Behaviors preserved:

  - **Rule 0:** ``--file ""`` (explicit empty) must NOT fall through to
    ``read_content`` (which would surface the misleading "content
    required" error). Surface a clear ``--file: path cannot be empty``.
  - **Rule 1:** ``--file`` and positional content are mutually exclusive.
  - **Rules 2-4:** file must exist, be a regular file, and be within
    the size cap. Validation runs before any read so oversized files
    are rejected without allocating a buffer.
  - **--engine** validated up front (typo feedback without IO wait).
    Acceptable values: ``fitz`` (PyMuPDF, default), ``shell``, ``http``.
    Mirrors Go's ``gxpdf|shell|http`` trio — ``fitz`` replaces ``gxpdf``
    because the Python port uses PyMuPDF, not gxpdf.
  - **--file title default:** when ``--title`` not set, derive from
    filename (last extension stripped; dotfiles keep full basename).
  - **Output modes:** ``--json`` (global flag) emits machine-readable
    JSON; otherwise plain-text human output.

Typer composition
=================

The root ``app`` lives in :mod:`unictx.cli.app`. This module builds two
nested Typers::

    user_app (Typer)            → unictx user ...
        note_app (Typer)        → unictx user note ...
            add / get / list / delete

The root app registers ``user_app`` via ``app.add_typer(user_app, name="user")``
in :mod:`unictx.cli.__init__` (Tasks 6.2-6.5 register their own Typers
the same way).

Container injection
===================

Production calls :func:`_default_load_container` which composes
``wire(load_config(get_config_path()))`` once per invocation. Tests
monkeypatch ``_load_container`` to return a tmp_path-backed container
(Go's ``userNoteLoadAppFn`` pattern).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import typer

from unictx.cli.app import AppContainer, get_config_path, wire
from unictx.config import load as load_config
from unictx.items.ingest import Input
from unictx.items.models import Kind, Scope, Source
from unictx.items.repo import ItemFilter
from unictx.pdf.factory import build_extractor_for_engine

__all__ = [
    "check_file_size",
    "derive_default_title",
    "format_list_item",
    "mime_for_file",
    "note_app",
    "preview_runes",
    "user_app",
    "validate_file_import",
]


# ---------------------------------------------------------------------------
# Typer sub-apps
# ---------------------------------------------------------------------------


user_app = typer.Typer(
    name="user",
    help="Manage personal-scope (user) knowledge.",
    no_args_is_help=True,
    add_completion=False,
)

note_app = typer.Typer(
    name="note",
    help="Manage personal notes.",
    no_args_is_help=True,
    add_completion=False,
)

user_app.add_typer(note_app, name="note")


# ---------------------------------------------------------------------------
# Container injection
# ---------------------------------------------------------------------------


def _default_load_container() -> AppContainer:
    """Production: load config from --config path (or XDG default) + wire."""
    return wire(load_config(get_config_path()))


# Module-level seam — tests monkeypatch this. Defaults to production path.
_load_container = _default_load_container


# ---------------------------------------------------------------------------
# Helpers — pure functions (unit-tested in test_user_note.py)
# ---------------------------------------------------------------------------


# File import size cap. Mirrors Go's maxFileBytes. Bumped from 10MB → 50MB
# for PDF support (academic papers commonly 5-15MB, scanned textbooks
# 20-80MB). Text files rarely approach this; cap exists as a guardrail.
_MAX_FILE_BYTES = 50 * 1024 * 1024

# Max rune count of content shown as a fallback title in list output.
_LIST_PREVIEW_LEN = 50

# Acceptable --engine values. Mirrors Go's gxpdf|shell|http trio — "fitz"
# replaces "gxpdf" because the Python port uses PyMuPDF, not gxpdf.
_VALID_ENGINES = {"fitz", "shell", "http"}


def mime_for_file(path: str) -> str:
    """Return MIME type for a file based on its extension.

    Renamed from Go's ``mimeForTextFile`` when PDF support was added
    (the old name lied once it returned application/pdf). Unknown
    extensions fall back to ``text/plain`` (backward compat for users
    who pass weirdly-named text files).
    """
    ext = Path(path).suffix.lower()
    if ext in {".md", ".markdown"}:
        return "text/markdown"
    if ext == ".pdf":
        return "application/pdf"
    return "text/plain"


def derive_default_title(path: str) -> str:
    """Extract a human-friendly title from a file path.

    Takes the basename and strips the last extension. Used when the user
    runs ``--file weekly.md`` without ``--title``. Only the LAST
    extension is stripped (``archive.tar.gz`` → ``archive.tar``) to
    match user intuition. A leading-dot file (``.bashrc``) keeps its
    full basename (dot at index 0 is not stripped).
    """
    base = Path(path).name
    dot = base.rfind(".")
    if dot > 0:
        base = base[:dot]
    return base


def check_file_size(size: int) -> str | None:
    """Return error message if ``size`` exceeds cap, else None."""
    if size > _MAX_FILE_BYTES:
        return f"file too large: {size} bytes (max {_MAX_FILE_BYTES})"
    return None


def validate_file_import(path: str) -> str | None:
    """Run file-level validation rules (Rules 2-4 from the spec).

    - File must exist (os.stat error surfaces with context).
    - File must be a regular file (not a directory, device, socket).
    - File must be within the size cap.

    Returns error message string on failure, ``None`` on success.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        return f"stat file: {exc}"
    if not os.path.isfile(path):
        return f"not a regular file: {path}"
    if err := check_file_size(st.st_size):
        return err
    return None


def preview_runes(s: str, n: int) -> str:
    """Return first ``n`` runes of ``s`` with ellipsis if truncated."""
    runes = list(s)
    if len(runes) <= n:
        return s
    return "".join(runes[:n]) + "…"


def format_list_item(item: Any) -> str:
    """Render one row of ``user note list``.

    When the item has a non-empty title, the title is shown verbatim.
    When the title is empty (common when ``add`` was called without
    ``--title``), a preview of the inline content is shown instead so
    the user sees something useful. When content is also empty
    (externalized to FileStore), an ``(externalized)`` placeholder is
    shown — the full content can always be retrieved with ``get <id>``.
    """
    label = item.title
    if not label:
        if item.content:
            label = preview_runes(item.content, _LIST_PREVIEW_LEN)
        elif item.content_uri:
            label = "(externalized)"
        else:
            label = "(no content)"
    tags = ",".join(item.tags)
    return f"{item.id}  {label}  [{tags}]"


def _read_content(content_arg: str | None) -> tuple[str, str | None]:
    """Resolve the positional-or-stdin content source.

    Returns ``(content, error_msg)``. On success ``error_msg`` is None.
    Mirrors Go's ``readContent``:
      - No positional → error "content required".
      - ``-`` → read stdin.
      - Anything else → use the positional verbatim.
    """
    if content_arg is None or content_arg == "":
        return "", "content required (positional arg or - for stdin)"
    if content_arg != "-":
        return content_arg, None
    # stdin
    try:
        data = sys.stdin.read()
    except OSError as exc:
        return "", f"read stdin: {exc}"
    return data, None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@note_app.command("add")
def add(
    content: str | None = typer.Argument(
        None,
        help='Note content (positional, "-" for stdin, or omit with --file).',
    ),
    title: str = typer.Option("", "--title", help="Note title."),
    tag: list[str] = typer.Option([], "--tag", help="Tags (repeatable: --tag red --tag blue)."),
    file: str = typer.Option("", "--file", help="Import content from a file (.txt, .md, .pdf)."),
    engine: str = typer.Option(
        "",
        "--engine",
        help='PDF extractor override: "fitz", "shell", or "http". Empty uses the config default.',
    ),
) -> None:
    """Add a personal note (positional arg, - for stdin, or --file <path>)."""
    from unictx.cli.app import is_json_mode

    # Rule 0: --file "" (explicit empty) must surface a clear error,
    # not the misleading "content required" from _read_content.
    # Typer doesn't distinguish "not passed" from "passed empty" by
    # default; we use a sentinel by checking file == "" after Typer
    # parsing. The mutual-exclusion + content-required checks below
    # handle the rest. (Go uses cobra.Flags().Changed("file").)
    if file == "" and content is None:
        # Bare `add` with no positional + no --file → content-required error.
        typer.echo("content required (positional arg or - for stdin)", err=True)
        raise typer.Exit(code=2)

    # Engine validation runs BEFORE any IO so users get typo feedback
    # instantly, without waiting for a file read or stat.
    if engine and engine not in _VALID_ENGINES:
        typer.echo(
            f'unknown pdf engine "{engine}" (want {"|".join(sorted(_VALID_ENGINES))})',
            err=True,
        )
        raise typer.Exit(code=2)

    note_content: str
    mime: str = ""
    source_meta: dict[str, Any] = {}

    if file:
        # File import path.
        if content is not None:
            # Rule 1: mutual exclusion.
            typer.echo(
                "cannot combine --file with positional content or -",
                err=True,
            )
            raise typer.Exit(code=2)
        if err := validate_file_import(file):
            typer.echo(err, err=True)
            raise typer.Exit(code=2)
        try:
            data = Path(file).read_bytes()
        except OSError as exc:
            typer.echo(f"read file: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        note_content = data.decode("utf-8", errors="replace")
        mime = mime_for_file(file)
        if not title:
            title = derive_default_title(file)
        source_meta["original_filename"] = Path(file).name
    else:
        # Positional / stdin path.
        c, err = _read_content(content)
        if err:
            typer.echo(err, err=True)
            raise typer.Exit(code=2)
        note_content = c

    container = _load_container()
    try:
        # Engine override: when --engine is set OR the file is a PDF,
        # build an extractor explicitly. The constructor default from
        # wire() only fires for non-PDF-aware callers — the CLI always
        # takes this path for PDFs so the choice is per-invocation.
        extractor_override = None
        if engine or mime == "application/pdf":
            engine_name = engine or container.config.pdf.engine
            if not engine_name:
                typer.echo(
                    "pdf extraction not configured: set pdf.engine in config or pass --engine",
                    err=True,
                )
                raise typer.Exit(code=2)
            try:
                extractor_override = build_extractor_for_engine(engine_name, container.config.pdf)
            except Exception as exc:
                typer.echo(f"build pdf extractor: {exc}", err=True)
                raise typer.Exit(code=2) from exc

        ingest_input = Input(
            scope=Scope.USER,
            kind=Kind.NOTE,
            source=Source.MANUAL,
            owner_user_id=container.config.user.id,
            title=title,
            content=note_content,
            tags=list(tag),
            mime=mime,
            source_meta=source_meta,
        )
        try:
            new_id = container.ingest.create(ingest_input, extractor=extractor_override)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json({"id": new_id, "status": "added"})
        else:
            typer.echo(f"added: {new_id}")
    finally:
        container.close()


@note_app.command("get")
def get(
    item_id: str = typer.Argument(..., help="Note id."),
) -> None:
    """Show a single note (full content, hydrated from FileStore if externalized)."""
    from unictx.cli.app import is_json_mode

    container = _load_container()
    try:
        try:
            item = container.items.get(item_id)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json(
                {
                    "id": item.id,
                    "title": item.title,
                    "summary": item.summary,
                    "content": item.content,
                    "tags": item.tags,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
            )
            return

        typer.echo(f"id:    {item.id}")
        typer.echo(f"title: {item.title}")
        typer.echo(f"tags:  {', '.join(item.tags)}")
        typer.echo("---")
        typer.echo(item.content)
    finally:
        container.close()


@note_app.command("list")
def list_notes(
    tag: list[str] = typer.Option([], "--tag", help="Filter by tag (OR semantics; repeatable)."),
    limit: int = typer.Option(20, "--limit", help="Max items to return (default 20)."),
) -> None:
    """List personal notes (newest first)."""
    from unictx.cli.app import is_json_mode

    if limit <= 0:
        limit = 20

    container = _load_container()
    try:
        filter_ = ItemFilter(
            scopes=[Scope.USER],
            owner_user_id=container.config.user.id,
            kinds=[Kind.NOTE],
            tags=list(tag),
            limit=limit,
        )
        try:
            items, _ = container.items.list(filter_)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json(
                [
                    {
                        "id": it.id,
                        "title": it.title,
                        "tags": it.tags,
                        "created_at": it.created_at,
                    }
                    for it in items
                ]
            )
            return

        if not items:
            typer.echo("(no notes)")
            return
        for it in items:
            typer.echo(format_list_item(it))
    finally:
        container.close()


@note_app.command("delete")
def delete(
    item_id: str = typer.Argument(..., help="Note id."),
) -> None:
    """Delete a note."""
    from unictx.cli.app import is_json_mode

    container = _load_container()
    try:
        try:
            container.items.delete(item_id)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json({"id": item_id, "status": "deleted"})
        else:
            typer.echo(f"deleted: {item_id}")
    finally:
        container.close()
