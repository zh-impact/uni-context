"""Tests for cli/access_cmd.py — access grant add/list/remove.

Two layers (mirrors test_embed_cmd.py):

  - **Pure helpers** (``validate_grant_add_args``, ``format_grant_row``):
    tested directly.
  - **End-to-end via CliRunner** against a tmp_path container wired with
    embedder disabled (grants are always available, independent of the
    embedder). Includes the load-bearing grant→search closure test that
    proves a grant actually widens a PROJECT actor's visible scope.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import unictx.cli.access_cmd as access_mod
from unictx.cli.access_cmd import format_grant_row, validate_grant_add_args
from unictx.cli.app import app as root_app
from unictx.config import Config, EmbedderConfig
from unictx.items.models import AccessGrant, Scope

# ---------------------------------------------------------------------------
# Container fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def container_factory(tmp_path: Path, monkeypatch):
    """Wire a fresh container per call; monkeypatch into the access module.

    Grants are independent of the embedder, so embedder.enabled=False is
    fine (keeps the container light). Each call re-wires so writes from
    prior commands are persisted on the shared tmp_path DB.
    """
    created: list = []

    def _make():
        cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
        c = access_mod.wire(cfg)
        created.append(c)
        return c

    monkeypatch.setattr(access_mod, "_load_container", _make)
    yield _make
    for c in created:
        c.close()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_validate_grant_add_args_rejects_user() -> None:
    """--as user is not grantable (user sees everything by default)."""
    err = validate_grant_add_args("user", "global")
    assert err is not None
    assert "not grantable" in err


def test_validate_grant_add_args_rejects_invalid_as() -> None:
    """An unknown --as value surfaces a clear error."""
    err = validate_grant_add_args("bogus", "global")
    assert err is not None
    assert "bogus" in err


def test_validate_grant_add_args_rejects_invalid_target() -> None:
    """An unknown --target value surfaces a clear error."""
    err = validate_grant_add_args("project", "bogus")
    assert err is not None
    assert "bogus" in err


def test_validate_grant_add_args_accepts_valid() -> None:
    """A well-formed (project/global, valid target) combination passes."""
    assert validate_grant_add_args("project", "user") is None
    assert validate_grant_add_args("global", "project") is None


def test_format_grant_row_all_projects_marker() -> None:
    """Empty project_id renders as '*' (the 'all projects' marker)."""
    g = AccessGrant(as_scope=Scope.PROJECT, project_id="", target_scope=Scope.USER)
    row = format_grant_row(7, g)
    parts = row.split("\t")
    assert parts == ["7", "project", "*", "user", ""]


def test_format_grant_row_specific_project() -> None:
    """A specific project_id renders verbatim."""
    g = AccessGrant(
        as_scope=Scope.PROJECT, project_id="P", target_scope=Scope.USER, reason="audited"
    )
    row = format_grant_row(3, g)
    assert row == "3\tproject\tP\tuser\taudited"


# ---------------------------------------------------------------------------
# Integration via CliRunner
# ---------------------------------------------------------------------------


def test_grant_add_then_list(container_factory) -> None:
    """grant add inserts a row that grant list surfaces."""
    runner = CliRunner()
    result = runner.invoke(
        root_app,
        [
            "access",
            "grant",
            "add",
            "--as",
            "project",
            "--project",
            "P",
            "--target",
            "user",
            "--reason",
            "cross-team",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "granted" in result.output

    result = runner.invoke(root_app, ["access", "grant", "list"])
    assert result.exit_code == 0, result.output
    assert "project" in result.output
    assert "user" in result.output
    assert "cross-team" in result.output


def test_grant_add_rejects_user(container_factory) -> None:
    """--as user → exit 2 with the not-grantable message."""
    runner = CliRunner()
    result = runner.invoke(
        root_app,
        ["access", "grant", "add", "--as", "user", "--target", "global"],
    )
    assert result.exit_code == 2
    assert "not grantable" in result.output


def test_grant_add_rejects_invalid_target(container_factory) -> None:
    """--target bogus → exit 2."""
    runner = CliRunner()
    result = runner.invoke(
        root_app,
        ["access", "grant", "add", "--as", "project", "--target", "bogus"],
    )
    assert result.exit_code == 2
    assert "bogus" in result.output


def test_grant_add_all_projects_when_project_omitted(container_factory) -> None:
    """Omitting --project creates an all-projects grant (renders as '*')."""
    runner = CliRunner()
    result = runner.invoke(
        root_app,
        ["access", "grant", "add", "--as", "global", "--target", "project"],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(root_app, ["access", "grant", "list"])
    assert result.exit_code == 0, result.output
    assert "*" in result.output  # all-projects marker


def test_grant_list_empty(container_factory) -> None:
    """grant list on an empty table prints '(no grants)'."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["access", "grant", "list"])
    assert result.exit_code == 0, result.output
    assert "no grants" in result.output


def test_grant_list_json(container_factory) -> None:
    """--json emits the grants envelope."""
    runner = CliRunner()
    runner.invoke(
        root_app,
        ["access", "grant", "add", "--as", "project", "--project", "P", "--target", "user"],
    )
    result = runner.invoke(root_app, ["--json", "access", "grant", "list"])
    assert result.exit_code == 0, result.output
    import json as _json

    out = result.output.strip()
    brace = out.index("{")
    payload = _json.loads(out[brace:])
    assert payload["grants"]
    g = payload["grants"][0]
    assert g["as_scope"] == "project"
    assert g["target_scope"] == "user"
    assert "id" in g


def test_grant_remove_then_list(container_factory) -> None:
    """grant remove deletes the row; subsequent list omits it."""
    runner = CliRunner()
    # Add a grant and capture its id from the JSON payload.
    add = runner.invoke(
        root_app,
        [
            "--json", "access", "grant", "add",
            "--as", "project", "--project", "P", "--target", "user",
        ],
    )
    import json as _json

    brace = add.output.index("{")
    gid = _json.loads(add.output[brace:])["id"]

    rm = runner.invoke(root_app, ["access", "grant", "remove", str(gid)])
    assert rm.exit_code == 0, rm.output
    assert "revoked" in rm.output

    listing = runner.invoke(root_app, ["access", "grant", "list"])
    assert listing.exit_code == 0, listing.output
    assert "no grants" in listing.output


def test_grant_remove_missing_id_is_noop(container_factory) -> None:
    """Revoking a non-existent id exits 0 (idempotent)."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["access", "grant", "remove", "9999"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Load-bearing closure: a grant actually widens a search actor's scope.
# ---------------------------------------------------------------------------


def test_grant_unlocks_search_for_project_actor(container_factory) -> None:
    """A grant lets a PROJECT actor search see USER-scope data.

    This is the integration proof that the management CLI and the read
    path are wired together: add a grant via the CLI, seed a user-scope
    item via the normal note-add path (so FTS indexes it properly), and
    confirm a `search --as project` now returns it (it would NOT without
    the grant).

    The user-note add path is used for seeding (rather than a direct
    repo.create) because ingest writes content through the pipeline the
    FTS external-content table expects — matching how real user data is
    stored. The test's _load_container seam is shared across commands so
    both `user note add` and `search` see the same tmp_path DB.

    NOTE: `search` has its OWN _load_container seam (cli/search.py),
    distinct from this module's. We must patch BOTH so grant, note-add,
    and search all hit the same tmp_path-backed container.
    """
    import unictx.cli.search as search_mod
    from unictx.cli import user_note

    runner = CliRunner()
    # The three command modules each have a _load_container seam; point
    # all three at the access module's patched factory so they share one
    # tmp_path DB.
    shared = access_mod._load_container
    user_note._load_container = shared
    search_mod._load_container = shared
    try:
        seed = runner.invoke(root_app, ["user", "note", "add", "secret user note"])
        assert seed.exit_code == 0, seed.output

        # 1. Without a grant: project actor cannot see the user note.
        pre = runner.invoke(
            root_app,
            ["search", "secret", "--as", "project", "--project", "P"],
        )
        assert pre.exit_code == 0, pre.output
        assert "no matches" in pre.output

        # 2. Grant project P access to user scope.
        grant = runner.invoke(
            root_app,
            ["access", "grant", "add", "--as", "project", "--project", "P", "--target", "user"],
        )
        assert grant.exit_code == 0, grant.output

        # 3. With the grant: project actor now finds the user note.
        post = runner.invoke(
            root_app,
            ["search", "secret", "--as", "project", "--project", "P"],
        )
        assert post.exit_code == 0, post.output
        # The user-scope item is now returned (it was blocked before the
        # grant). user note add leaves the title empty for inline content,
        # so we assert on the result row presence (id-prefix marker +
        # scope=user) rather than the query term in the title.
        assert "[0" in post.output, f"no result row: {post.output!r}"
        assert "scope=user" in post.output
    finally:
        user_note._load_container = user_note._default_load_container
        search_mod._load_container = search_mod._default_load_container
