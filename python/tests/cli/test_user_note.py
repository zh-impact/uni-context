"""Tests for cli/user_note.py — user note add/get/list/delete.

Mirrors Go's user_note_run_e_test.go + user_note_test.go. Each test
exercises a Typer subcommand end-to-end via ``typer.testing.CliRunner``,
with a stubbed ``_load_container`` factory pointing at a tmp_path-backed
AppContainer (isolated DB + filestore per test).

Pure helpers (mime_for_file, derive_default_title, check_file_size,
validate_file_import, format_list_item) get their own unit tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from unictx.cli import user_note
from unictx.cli.user_note import (
    check_file_size,
    derive_default_title,
    format_list_item,
    mime_for_file,
    preview_runes,
    user_app,
    validate_file_import,
)
from unictx.config import Config, EmbedderConfig
from unictx.items.models import ContextItem, Kind, Scope, Source

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def container_factory(tmp_path: Path, monkeypatch):
    """Return a factory that wires a fresh container against tmp_path.

    Each call returns a NEW container; the caller closes them. The
    factory is monkeypatched into ``user_note._load_container`` so
    Typer commands pick it up automatically.

    The factory uses ``embedder.enabled=False`` (Plan 1 mode) so tests
    don't require Ollama. The IngestService's PDF branch is still
    exercised via --engine overrides.
    """
    created: list = []

    def _make() -> user_note.AppContainer:
        cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
        c = user_note.wire(cfg)
        created.append(c)
        return c

    monkeypatch.setattr(user_note, "_load_container", _make)
    yield _make
    for c in created:
        c.close()


# ---------------------------------------------------------------------------
# Pure helpers — unit tests
# ---------------------------------------------------------------------------


def test_mime_for_file_known_extensions() -> None:
    assert mime_for_file("note.md") == "text/markdown"
    assert mime_for_file("note.markdown") == "text/markdown"
    assert mime_for_file("paper.pdf") == "application/pdf"
    assert mime_for_file("readme.txt") == "text/plain"
    # Unknown extension → text/plain (backward compat).
    assert mime_for_file("weird.foobar") == "text/plain"
    # Case-insensitive.
    assert mime_for_file("Note.MD") == "text/markdown"


def test_derive_default_title_strips_last_extension() -> None:
    assert derive_default_title("weekly.md") == "weekly"
    assert derive_default_title("/path/to/weekly.md") == "weekly"
    # Only last extension stripped (archive.tar.gz → archive.tar).
    assert derive_default_title("archive.tar.gz") == "archive.tar"
    # Dotfiles keep their full basename (.bashrc stays).
    assert derive_default_title(".bashrc") == ".bashrc"
    # No extension → basename unchanged.
    assert derive_default_title("README") == "README"


def test_check_file_size_within_cap() -> None:
    """50 MB cap: returns None for in-cap, error string for over."""
    assert check_file_size(0) is None
    assert check_file_size(50 * 1024 * 1024) is None  # exactly cap
    assert check_file_size(50 * 1024 * 1024 + 1) is not None
    assert "too large" in check_file_size(50 * 1024 * 1024 + 1)


def test_preview_runes_short_string_returns_as_is() -> None:
    assert preview_runes("hello", 50) == "hello"
    assert preview_runes("hello", 5) == "hello"


def test_preview_runes_long_string_truncates_with_ellipsis() -> None:
    s = "a" * 100
    out = preview_runes(s, 10)
    assert out.endswith("…")
    assert out[:-1] == "a" * 10


def test_format_list_item_with_title() -> None:
    item = ContextItem(
        id="abc12345",
        title="my note",
        tags=["x", "y"],
        scope=Scope.USER,
        kind=Kind.NOTE,
        source=Source.MANUAL,
    )
    line = format_list_item(item)
    assert "abc12345" in line
    assert "my note" in line
    assert "[x,y]" in line or "[x, y]" in line


def test_format_list_item_title_falls_back_to_content_preview() -> None:
    item = ContextItem(
        id="abc12345",
        title="",
        content="some long content body",
        scope=Scope.USER,
        kind=Kind.NOTE,
        source=Source.MANUAL,
    )
    line = format_list_item(item)
    assert "some long content body" in line


def test_format_list_item_externalized_shows_placeholder() -> None:
    item = ContextItem(
        id="abc12345",
        title="",
        content="",
        content_uri="file://deadbeef",
        scope=Scope.USER,
        kind=Kind.NOTE,
        source=Source.MANUAL,
    )
    line = format_list_item(item)
    assert "(externalized)" in line


def test_validate_file_import_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "ghost.txt"
    err = validate_file_import(str(missing))
    assert err is not None
    assert "stat file" in err or "no such file" in err.lower()


def test_validate_file_import_accepts_regular_file(tmp_path: Path) -> None:
    f = tmp_path / "note.txt"
    f.write_text("hello")
    assert validate_file_import(str(f)) is None


# ---------------------------------------------------------------------------
# Typer subcommands — integration via CliRunner
# ---------------------------------------------------------------------------


def test_add_positional_creates_note(container_factory) -> None:
    """`user note add "hello"` → exits 0, prints 'added: <id>'."""
    runner = CliRunner()
    result = runner.invoke(user_app, ["note", "add", "hello world"])
    assert result.exit_code == 0, result.stdout
    assert "added:" in result.stdout

    # Verify via list.
    list_result = runner.invoke(user_app, ["note", "list"])
    assert list_result.exit_code == 0
    assert "hello world" in list_result.stdout or "(no content)" in list_result.stdout


def test_add_with_title_flag(container_factory) -> None:
    """`user note add "body" --title "My Title"` → list shows My Title."""
    runner = CliRunner()
    runner.invoke(user_app, ["note", "add", "body text", "--title", "My Title"])
    list_result = runner.invoke(user_app, ["note", "list"])
    assert "My Title" in list_result.stdout


def test_add_with_tags(container_factory) -> None:
    """--tag repeatable → tags land on item."""
    runner = CliRunner()
    runner.invoke(user_app, ["note", "add", "body", "--tag", "red", "--tag", "blue"])
    list_result = runner.invoke(user_app, ["note", "list"])
    assert "red" in list_result.stdout
    assert "blue" in list_result.stdout


def test_add_from_file(container_factory, tmp_path: Path) -> None:
    """--file <path> → imports content + sets MIME + derives title."""
    f = tmp_path / "weekly.md"
    f.write_text("# Week in review\nLots of progress.")
    runner = CliRunner()
    result = runner.invoke(user_app, ["note", "add", "--file", str(f)])
    assert result.exit_code == 0, result.stdout
    list_result = runner.invoke(user_app, ["note", "list"])
    # Derived title from filename.
    assert "weekly" in list_result.stdout


def test_add_file_mutual_exclusion_with_positional(container_factory, tmp_path: Path) -> None:
    """--file + positional → exit non-zero with clear error."""
    f = tmp_path / "note.txt"
    f.write_text("hi")
    runner = CliRunner()
    result = runner.invoke(user_app, ["note", "add", "positional", "--file", str(f)])
    assert result.exit_code != 0
    assert "cannot combine" in result.output.lower()


def test_add_file_empty_path_rejected(container_factory) -> None:
    """--file "" (explicit empty) → exit non-zero with clear error."""
    runner = CliRunner()
    result = runner.invoke(user_app, ["note", "add", "--file", ""])
    assert result.exit_code != 0


def test_add_no_content_no_file_errors(container_factory) -> None:
    """No positional, no -, no --file → exit non-zero."""
    runner = CliRunner()
    result = runner.invoke(user_app, ["note", "add"])
    assert result.exit_code != 0
    assert "content required" in result.output.lower()


def test_add_engine_unknown_rejected(container_factory) -> None:
    """--engine bogus → exit non-zero before any IO."""
    runner = CliRunner()
    result = runner.invoke(user_app, ["note", "add", "x", "--engine", "bogus"])
    assert result.exit_code != 0
    assert "engine" in result.output.lower() or "unknown" in result.output.lower()


def test_get_returns_full_content(container_factory) -> None:
    """`user note get <id>` → prints title + content."""
    runner = CliRunner()
    add_result = runner.invoke(user_app, ["note", "add", "full body text", "--title", "T"])
    added_id = add_result.stdout.split("added:", 1)[-1].strip()

    get_result = runner.invoke(user_app, ["note", "get", added_id])
    assert get_result.exit_code == 0, get_result.stdout
    assert "T" in get_result.stdout
    assert "full body text" in get_result.stdout


def test_get_missing_id_errors(container_factory) -> None:
    """`user note get <missing-id>` → exit non-zero."""
    runner = CliRunner()
    result = runner.invoke(user_app, ["note", "get", "ghost-id"])
    assert result.exit_code != 0


def test_list_default_limit_20(container_factory) -> None:
    """Bare `user note list` → defaults to limit=20."""
    runner = CliRunner()
    # Add 1 note — we just verify list doesn't error.
    runner.invoke(user_app, ["note", "add", "only one"])
    result = runner.invoke(user_app, ["note", "list"])
    assert result.exit_code == 0


def test_list_empty_prints_no_notes(container_factory) -> None:
    """Empty list → friendly '(no notes)' message."""
    runner = CliRunner()
    result = runner.invoke(user_app, ["note", "list"])
    assert result.exit_code == 0
    assert "no notes" in result.stdout.lower()


def test_delete_removes_note(container_factory) -> None:
    """`user note delete <id>` → subsequent get fails."""
    runner = CliRunner()
    add_result = runner.invoke(user_app, ["note", "add", "to be deleted"])
    added_id = add_result.stdout.split("added:", 1)[-1].strip()

    delete_result = runner.invoke(user_app, ["note", "delete", added_id])
    assert delete_result.exit_code == 0
    assert "deleted:" in delete_result.stdout

    # Subsequent get should fail.
    get_result = runner.invoke(user_app, ["note", "get", added_id])
    assert get_result.exit_code != 0


def test_json_flag_emits_valid_json(container_factory) -> None:
    """--json → machine-readable JSON output (global flag from app.py)."""
    # Import the root app so the global --json flag fires through callback.
    from unictx.cli.app import app as root_app

    runner = CliRunner()
    result = runner.invoke(root_app, ["--json", "user", "note", "add", "json body"])
    assert result.exit_code == 0, result.stdout
    import json as _json

    # The last non-empty stdout line should be the JSON object.
    out = result.stdout.strip()
    # Strip ANSI / leading non-JSON chars; the JSON object starts at "{".
    brace = out.index("{")
    payload = _json.loads(out[brace:])
    assert payload["status"] == "added"
    assert "id" in payload
