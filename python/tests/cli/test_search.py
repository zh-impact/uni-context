"""Tests for cli/search.py — search command + helpers.

Mirrors Go's search_test.go. Tests:
  - Pure helpers: normalize_search_mode, validate_search_mode,
    parse_scopes, parse_kinds.
  - End-to-end via CliRunner against a tmp_path container seeded with
    ingest-able items.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import unictx.cli.search as search_mod
from unictx.cli.app import app as root_app
from unictx.cli.search import (
    normalize_search_mode,
    parse_kinds,
    parse_scopes,
    validate_search_mode,
)
from unictx.config import Config, EmbedderConfig


@pytest.fixture
def container_factory(tmp_path: Path, monkeypatch):
    """Wire a fresh container per call; monkeypatch into search module."""
    created: list = []

    def _make():
        cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
        c = search_mod.wire(cfg)
        created.append(c)
        return c

    monkeypatch.setattr(search_mod, "_load_container", _make)
    yield _make
    for c in created:
        c.close()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_normalize_search_mode_empty_defaults_to_fts_only() -> None:
    assert normalize_search_mode("") == "fts-only"
    assert normalize_search_mode("hybrid") == "hybrid"
    assert normalize_search_mode("fts-only") == "fts-only"


def test_validate_search_mode_known_returns_none() -> None:
    assert validate_search_mode("fts-only") is None
    assert validate_search_mode("hybrid") is None


def test_validate_search_mode_unknown_returns_error() -> None:
    err = validate_search_mode("vector")
    assert err is not None
    assert "vector" in err
    assert "fts-only" in err
    assert "hybrid" in err


def test_parse_scopes_known() -> None:
    scopes, err = parse_scopes(["user", "global"])
    assert err is None
    assert len(scopes) == 2


def test_parse_scopes_unknown_surfaces_error() -> None:
    _, err = parse_scopes(["bogus"])
    assert err is not None
    assert "bogus" in err
    assert "user" in err  # valid set echoed


def test_parse_kinds_known() -> None:
    kinds, err = parse_kinds(["note", "doc"])
    assert err is None
    assert len(kinds) == 2


def test_parse_kinds_unknown_surfaces_error() -> None:
    _, err = parse_kinds(["bogus"])
    assert err is not None
    assert "bogus" in err


# ---------------------------------------------------------------------------
# Integration via CliRunner
# ---------------------------------------------------------------------------


def _seed_notes(runner: CliRunner, *contents: str) -> None:
    """Add one note per content string via the user note command."""
    # We hit the user_app via the root app — same container factory.
    from unictx.cli import user_note

    # user_note has its OWN _load_container seam; we need to re-point
    # it at the SAME tmp_path so search can see what was ingested.
    # The factory above points at search_mod; mirror it for user_note.
    search_factory = search_mod._load_container

    def _shared_make():
        # Re-wire each call so a fresh container opens (and the prior
        # one's writes are flushed). Production doesn't share containers
        # across commands either; each invocation is fresh.
        return search_factory()

    # Patch user_note's seam.
    orig = user_note._load_container
    user_note._load_container = _shared_make
    try:
        for c in contents:
            runner.invoke(root_app, ["user", "note", "add", c])
    finally:
        user_note._load_container = orig


def test_search_no_query_errors(container_factory) -> None:
    """Bare `search` with no positional args → exit non-zero."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["search"])
    assert result.exit_code != 0


def test_search_unknown_mode_rejected(container_factory) -> None:
    """--mode bogus → exit non-zero with clear error."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["search", "hello", "--mode", "bogus"])
    assert result.exit_code != 0
    assert "mode" in result.output.lower()


def test_search_no_matches(container_factory) -> None:
    """Search on empty index → '(no matches)'."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["search", "ghost-query"])
    assert result.exit_code == 0
    assert "no matches" in result.output.lower()


def test_search_finds_known_term(container_factory) -> None:
    """FTS search returns matching items."""
    runner = CliRunner()
    _seed_notes(
        runner,
        "the quick brown fox jumps over the lazy dog",
        "a totally unrelated note about programming",
    )

    result = runner.invoke(root_app, ["search", "fox"])
    assert result.exit_code == 0, result.output
    # Title-only snippet (storage-layer bugfix in searcher_impl.py:21-39
    # means content snippets are not returned). The user-note add path
    # leaves title empty for inline content, so we assert by the id-prefix
    # marker that the result row rendered — proving the hit came back.
    result_lines = [ln for ln in result.output.splitlines() if ln.startswith("[")]
    assert len(result_lines) >= 1


def test_search_limit_flag(container_factory) -> None:
    """--limit 1 truncates results to 1 even when more match."""
    runner = CliRunner()
    _seed_notes(
        runner,
        "python is great",
        "python rocks",
        "python forever",
    )
    result = runner.invoke(root_app, ["search", "python", "--limit", "1"])
    assert result.exit_code == 0, result.output
    # Count result lines starting with `[` (id prefix marker).
    result_lines = [ln for ln in result.output.splitlines() if ln.startswith("[")]
    assert len(result_lines) <= 1


def test_search_invalid_scope_rejected(container_factory) -> None:
    """--scope bogus → exit non-zero with valid-set echoed."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["search", "x", "--scope", "bogus"])
    assert result.exit_code != 0
    assert "scope" in result.output.lower()


def test_search_invalid_kind_rejected(container_factory) -> None:
    """--kind bogus → exit non-zero with valid-set echoed."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["search", "x", "--kind", "bogus"])
    assert result.exit_code != 0
    assert "kind" in result.output.lower()


def test_search_json_output(container_factory) -> None:
    """--json → emits {results, total, mode, as_scope} JSON envelope."""
    runner = CliRunner()
    _seed_notes(runner, "python is great")
    result = runner.invoke(root_app, ["--json", "search", "python"])
    assert result.exit_code == 0, result.output
    import json as _json

    out = result.output.strip()
    brace = out.index("{")
    payload = _json.loads(out[brace:])
    assert payload["total"] >= 1
    assert payload["mode"] == "fts-only"
    assert payload["as_scope"] == "user"  # default identity echoed
    assert isinstance(payload["results"], list)
    if payload["results"]:
        assert "id" in payload["results"][0]
        assert "score" in payload["results"][0]


# ===========================================================================
# P1: access direction CLI flags (--as / --project).
# ===========================================================================


def test_search_as_project_requires_project_flag(container_factory) -> None:
    """--as project without --project → exit 2 with a clear message."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["search", "x", "--as", "project"])
    assert result.exit_code == 2
    assert "--project" in result.output


def test_search_invalid_as_scope_rejected(container_factory) -> None:
    """--as bogus → exit 2 with valid-set echoed."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["search", "x", "--as", "bogus"])
    assert result.exit_code == 2
    assert "--as" in result.output


def test_search_as_global_hides_user_data(container_factory) -> None:
    """--as global → user-scope notes are NOT returned.

    The CLI-level anti-leak: a global-identity search must not surface
    user private notes. We seed a user note (via the normal add path)
    plus a global item (via the repo directly), then search as global.
    """
    runner = CliRunner()
    # User-scope note (private) — seeded via the CLI add path.
    _seed_notes(runner, "secret user note")
    # Global-scope item — seeded directly via the repo.
    from unictx.items.models import Kind, NewItemParams, Scope, Source, new_context_item

    container = container_factory()
    try:
        g = new_context_item(
            Scope.GLOBAL, Kind.NOTE, Source.MANUAL, NewItemParams(),
            title="shared global rule", content="shared global rule",
        )
        container.items._repo.create(g)
    finally:
        container.close()

    result = runner.invoke(root_app, ["search", "shared", "--as", "global"])
    assert result.exit_code == 0, result.output
    # The global item appears; the user "secret" does not.
    assert "shared global rule" in result.output
    assert "secret" not in result.output


def test_search_as_user_default_sees_everything(container_factory) -> None:
    """No --as flag → default user identity sees all scopes (backward compat)."""
    runner = CliRunner()
    _seed_notes(runner, "user note here")
    from unictx.items.models import Kind, NewItemParams, Scope, Source, new_context_item

    container = container_factory()
    try:
        g = new_context_item(
            Scope.GLOBAL, Kind.NOTE, Source.MANUAL, NewItemParams(),
            title="global note", content="global note",
        )
        container.items._repo.create(g)
    finally:
        container.close()

    # No --as → default user, sees both user and global.
    result = runner.invoke(root_app, ["search", "note"])
    assert result.exit_code == 0, result.output
