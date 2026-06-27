"""End-to-end CLI flows — multi-command integration tests.

Mirrors Go's ``internal/cli/e2e_test.go`` (build tag ``e2e``). Each
test exercises a multi-command flow via ``typer.testing.CliRunner``
against a tmp_path-backed container, verifying the integration points
between subcommands that isolated unit tests can miss:

  - **Note lifecycle**: add → list → get → delete → search-miss.
  - **Large content externalization**: > 4KB content lands in
    FileStore; ``user note get`` hydrates it back; FTS still finds it.

These complement the per-command tests in ``test_user_note.py`` /
``test_search.py`` by exercising the seams between them (storage
visibility across separate ``wire()`` invocations, FileStore
hydration round-trip, FTS row visibility after delete).
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import unictx.cli.search as search_mod
import unictx.cli.user_note as user_note_mod
from unictx.cli.app import app as root_app
from unictx.config import Config, EmbedderConfig


@pytest.fixture
def shared_container_factory(tmp_path: Path, monkeypatch):
    """Wire BOTH user_note and search to the SAME tmp_path.

    Production CLI invocations are fresh processes — each command opens
    its own container. Tests need to mirror that: each ``runner.invoke``
    call wires a fresh container against the same on-disk DB so prior
    writes are visible. Both modules' ``_load_container`` seams are
    patched to the same factory.
    """
    created: list = []
    counter = [0]

    def _make():
        # Each invocation gets a fresh container — same as production
        # (each CLI call is a fresh process). The tmp_path persists
        # across calls so the SQLite DB + FileStore directory survive.
        counter[0] += 1
        cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
        c = user_note_mod.wire(cfg)
        created.append(c)
        return c

    monkeypatch.setattr(user_note_mod, "_load_container", _make)
    monkeypatch.setattr(search_mod, "_load_container", _make)
    yield _make
    for c in created:
        c.close()


# ---------------------------------------------------------------------------
# Note lifecycle: add → list → get → delete → search-miss
# ---------------------------------------------------------------------------


def test_note_lifecycle_add_list_get_delete_search(
    shared_container_factory,
) -> None:
    """Full lifecycle: add → list shows it → get returns content → delete
    removes → search no longer finds it.

    Mirrors Go's TestE2E_NoteLifecycleAndSearch. Verifies the storage
    layer's visibility across separate ``wire()`` calls (each command
    opens its own container — same as a fresh process per invocation).
    """
    runner = CliRunner()

    # Add note A with title + tags.
    result = runner.invoke(
        root_app,
        [
            "--json",
            "user",
            "note",
            "add",
            "How to deploy Go services",
            "--title",
            "Deploy Guide",
            "--tag",
            "go",
        ],
    )
    assert result.exit_code == 0, result.output
    out = result.output.strip()
    payload = _json.loads(out[out.index("{") :])
    note_a_id = payload["id"]
    assert note_a_id

    # Add note B (no title).
    result = runner.invoke(root_app, ["user", "note", "add", "Python scraping tutorial"])
    assert result.exit_code == 0, result.output

    # List shows 2 notes.
    result = runner.invoke(root_app, ["--json", "user", "note", "list"])
    assert result.exit_code == 0, result.output
    out = result.output.strip()
    list_payload = _json.loads(out[out.index("[") :])
    # The list output is a JSON array of note objects.
    assert len(list_payload) >= 2

    # Search "deploy" finds note A.
    result = runner.invoke(root_app, ["--json", "search", "deploy"])
    assert result.exit_code == 0, result.output
    out = result.output.strip()
    search_payload = _json.loads(out[out.index("{") :])
    assert search_payload["total"] >= 1
    # The matching result should be note A (the only "deploy" content).
    result_ids = [r["id"] for r in search_payload["results"]]
    assert note_a_id in result_ids

    # Get returns the title.
    result = runner.invoke(root_app, ["--json", "user", "note", "get", note_a_id])
    assert result.exit_code == 0, result.output
    out = result.output.strip()
    get_payload = _json.loads(out[out.index("{") :])
    assert get_payload["title"] == "Deploy Guide"

    # Delete note A.
    result = runner.invoke(root_app, ["user", "note", "delete", note_a_id])
    assert result.exit_code == 0, result.output

    # Search "deploy" now misses (total = 0).
    result = runner.invoke(root_app, ["--json", "search", "deploy"])
    assert result.exit_code == 0, result.output
    out = result.output.strip()
    search_payload = _json.loads(out[out.index("{") :])
    assert search_payload["total"] == 0


# ---------------------------------------------------------------------------
# Large content externalization round-trip
# ---------------------------------------------------------------------------


def test_large_content_externalized_get_returns_full(
    shared_container_factory,
) -> None:
    """Content > 4KB (``ContentInlineLimit``) externalizes to FileStore;
    ``user note get`` hydrates it back byte-for-byte.

    Mirrors Go's TestE2E_LargeContentExternalized. This is the load-
    bearing round-trip test for the externalization policy: if the
    FileStore path or hydration logic regresses, this fails.
    """
    runner = CliRunner()
    big = "long content word " * 500  # ~9.5KB, well above the 4KB cap

    # Add the big note via positional content (no --file).
    result = runner.invoke(
        root_app,
        ["--json", "user", "note", "add", big, "--title", "Big"],
    )
    assert result.exit_code == 0, result.output
    out = result.output.strip()
    payload = _json.loads(out[out.index("{") :])
    note_id = payload["id"]

    # Get returns the full content (hydration from FileStore succeeds).
    result = runner.invoke(root_app, ["--json", "user", "note", "get", note_id])
    assert result.exit_code == 0, result.output
    out = result.output.strip()
    get_payload = _json.loads(out[out.index("{") :])
    assert get_payload["content"] == big


def test_large_content_externalized_fts_searchable(
    shared_container_factory,
) -> None:
    """Externalized content's FTS row is populated by ReindexFTS —
    search finds the keyword even though it's not inline.

    Regression test for the historical bug where the AFTER INSERT
    trigger on context_item read ``new.content`` (which was empty for
    externalized items), leaving FTS rows indexed empty. IngestService
    now calls ReindexFTS on Create to fix this; the test ensures the
    fix stays in place.
    """
    runner = CliRunner()
    # A unique keyword repeated enough times to push past the 4KB cap.
    body = "uniqloid " * 600  # ~5.4KB

    result = runner.invoke(root_app, ["--json", "user", "note", "add", body])
    assert result.exit_code == 0, result.output

    # Search for the unique keyword.
    result = runner.invoke(root_app, ["--json", "search", "uniqloid"])
    assert result.exit_code == 0, result.output
    out = result.output.strip()
    payload = _json.loads(out[out.index("{") :])
    assert payload["total"] >= 1, "externalized content not searchable via FTS"


# ---------------------------------------------------------------------------
# Doctor + reindex-fts smoke (E2E)
# ---------------------------------------------------------------------------


def test_doctor_runs_clean_on_fresh_state(shared_container_factory) -> None:
    """``doctor`` exits 0 on a fresh DB — mirrors Go's e2e sanity check."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "schema version:" in result.output
