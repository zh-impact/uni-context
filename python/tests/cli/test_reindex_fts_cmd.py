"""Tests for cli/reindex_fts_cmd.py — reindex-fts command."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import unictx.cli.reindex_fts_cmd as reindex_mod
from unictx.cli.app import app as root_app
from unictx.config import Config, EmbedderConfig


@pytest.fixture
def container_factory(tmp_path: Path, monkeypatch):
    """Wire a per-call container; monkeypatch into reindex module."""
    created: list = []

    def _make():
        cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
        c = reindex_mod.wire(cfg)
        created.append(c)
        return c

    monkeypatch.setattr(reindex_mod, "_load_container", _make)
    yield _make
    for c in created:
        c.close()


def test_reindex_dry_run_empty_corpus(container_factory) -> None:
    """Dry-run on empty DB → 0 scanned, exit 0, 'dry run' message."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["reindex-fts", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert "0" in result.output


def test_reindex_run_empty_corpus(container_factory) -> None:
    """Non-dry-run on empty DB → 'reindex complete: reindexed=0 ...'."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["reindex-fts"])
    assert result.exit_code == 0, result.output
    assert "reindex complete" in result.output.lower()
    assert "reindexed=0" in result.output


def test_reindex_json_output(container_factory) -> None:
    """--json → {scanned, reindexed, failed, dry_run, failures} envelope."""
    import json as _json

    runner = CliRunner()
    result = runner.invoke(root_app, ["--json", "reindex-fts", "--dry-run"])
    assert result.exit_code == 0, result.output

    out = result.output.strip()
    brace = out.index("{")
    payload = _json.loads(out[brace:])
    assert payload["scanned"] == 0
    assert payload["reindexed"] == 0
    assert payload["failed"] == 0
    assert payload["dry_run"] is True
    assert payload["failures"] == []
