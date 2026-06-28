"""Tests for cli.app — Typer app skeleton + wire() container factory.

Plan §Task 6.1. Two concerns:

1. **wire(cfg)** — pure factory: opens DB, runs migrations, composes every
   service from concrete impls. The ONLY place that imports
   ``storage/*_impl.py`` directly (guard test in test_no_direct_storage_import.py
   exempts app.py).

2. **Typer app + global flags** — ``--config``, ``--json``, ``--verbose``
   parsed by the callback; subcommand files (Tasks 6.2-6.5) consume them
   via ``is_json_mode()`` / ``get_config_path()`` accessors.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from unictx.cli.app import (
    AppContainer,
    app,
    get_config_path,
    get_verbose,
    is_json_mode,
    wire,
)
from unictx.config import Config, EmbedderConfig
from unictx.embed.errors import ModelNotFound

# ---------------------------------------------------------------------------
# wire() — container factory
# ---------------------------------------------------------------------------


def test_wire_disabled_embedder_returns_partial_container(tmp_path: Path) -> None:
    """embedder.enabled=False → embed/models/backfill/worker/reembed are None.

    Plan 1 behavior: no vector pipeline. Other services (ingest, items,
    search, reindex_fts, diagnostics) are always constructed.
    """
    cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
    container = wire(cfg)
    try:
        assert isinstance(container, AppContainer)
        # Plan-1 nullable fields.
        assert container.embed is None
        assert container.models is None
        assert container.backfill is None
        assert container.worker is None
        assert container.reembed is None
        # Always-constructed services.
        assert container.ingest is not None
        assert container.items is not None
        assert container.search is not None
        assert container.reindex_fts is not None
        assert container.diagnostics is not None
    finally:
        container.close()


def test_wire_creates_data_dir_if_missing(tmp_path: Path) -> None:
    """wire() mkdir-p's cfg.data_dir; never assumes caller pre-created it."""
    missing = tmp_path / "does-not-exist-yet"
    cfg = Config(data_dir=missing, embedder=EmbedderConfig(enabled=False))
    container = wire(cfg)
    try:
        assert missing.exists()
        assert missing.is_dir()
        # FileStore root also created (mkdir-p inside FileStoreImpl.__init__).
        assert (missing / "filestore").is_dir()
    finally:
        container.close()


def test_wire_runs_migrations(tmp_path: Path) -> None:
    """Fresh DB has schema_meta row + context_item table after wire()."""
    cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
    container = wire(cfg)
    try:
        # DiagnosticService pulls schema_version from schema_meta.
        assert container.diagnostics.schema_version() == "4"
        # context_item table exists (migration 0001 creates it).
        row = container.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='context_item'"
        ).fetchone()
        assert row is not None
    finally:
        container.close()


def test_wire_close_releases_db(tmp_path: Path) -> None:
    """close() closes the DB connection; further queries raise."""
    cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
    container = wire(cfg)
    container.close()
    with pytest.raises(sqlite3.ProgrammingError):
        # "Cannot operate on a closed database"
        container.db.execute("SELECT 1").fetchone()


def test_wire_reconcile_registers_cfg_model_when_missing(
    tmp_path: Path,
) -> None:
    """embedder.enabled=True + cfg.model not in registry → wire()
    auto-registers it (Plan 2c self-heal).

    Without reconcile, this scenario raised ModelNotFound and the user
    had to run `embed model add` first. With reconcile, the cfg-driven
    model is registered automatically, including being set as default
    (no other default existed).
    """
    # Phase 1: wire disabled + wipe the seeded default model so the
    # registry is empty before reconcile runs.
    setup_cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
    setup_container = wire(setup_cfg)
    setup_container.db.execute("DELETE FROM embedding_model")
    setup_container.close()

    # Phase 2: enable embedder with a custom model in cfg.
    cfg = Config(
        data_dir=tmp_path,
        embedder=EmbedderConfig(
            enabled=True,
            provider="openai-compat",
            model="custom-foo",
            dimension=1024,
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
        ),
    )
    container = wire(cfg)
    try:
        # The custom model was registered + set as default.
        row = container.db.execute(
            "SELECT slug, is_default FROM embedding_model WHERE slug = ?",
            ("custom-foo",),
        ).fetchone()
        assert row is not None, "reconcile should have registered 'custom-foo'"
        assert row[1] == 1, "reconcile should set custom-foo as default"
    finally:
        container.close()


def test_wire_reconcile_idempotent(tmp_path: Path) -> None:
    """Calling wire() twice with the same cfg → second call no-ops.

    The second wire() finds the model already registered and skips the
    INSERT (otherwise it would ModelConflict). No default flip either.
    """
    # Wipe the seed so reconcile has work to do on the first wire.
    setup_cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
    setup_container = wire(setup_cfg)
    setup_container.db.execute("DELETE FROM embedding_model")
    setup_container.close()

    cfg = Config(
        data_dir=tmp_path,
        embedder=EmbedderConfig(
            enabled=True,
            provider="openai-compat",
            model="custom-foo",
            dimension=1024,
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
        ),
    )
    c1 = wire(cfg)
    c1.close()
    c2 = wire(cfg)
    try:
        rows = c2.db.execute(
            "SELECT slug FROM embedding_model WHERE slug = 'custom-foo'"
        ).fetchall()
        assert len(rows) == 1, "second wire() must not duplicate the row"
    finally:
        c2.close()


def test_wire_reconcile_skips_when_model_exists(tmp_path: Path) -> None:
    """If the cfg model is already registered, reconcile is a no-op.

    Pre-register 'custom-foo' with one set of fields; wire() with
    cfg.model='custom-foo' must NOT overwrite the existing row.
    """
    # Pre-register custom-foo directly via SQL with sentinel api_key.
    setup_cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
    setup_container = wire(setup_cfg)
    setup_container.db.execute("DELETE FROM embedding_model")
    setup_container.db.execute(
        "INSERT INTO embedding_model "
        "(slug, name, provider, dimension, vec_table, is_default, status, config, created_at) "
        "VALUES ('custom-foo', 'custom-foo', 'openai-compat', 1024, 'vec_custom_foo_1024', "
        "1, 'active', '{\"api_key\":\"SENTINEL\"}', strftime('s','now'))"
    )
    setup_container.close()

    cfg = Config(
        data_dir=tmp_path,
        embedder=EmbedderConfig(
            enabled=True,
            provider="openai-compat",
            model="custom-foo",
            dimension=1024,
            base_url="http://localhost:1234/v1",
            api_key="sk-different",
        ),
    )
    container = wire(cfg)
    try:
        row = container.db.execute(
            "SELECT config FROM embedding_model WHERE slug = 'custom-foo'"
        ).fetchone()
        assert row is not None
        assert "SENTINEL" in row[0], "existing row must not be overwritten"
        assert "sk-different" not in row[0]
    finally:
        container.close()


def test_wire_reconcile_does_not_override_user_default(tmp_path: Path) -> None:
    """If a different model is already default, reconcile registers the
    cfg model but does NOT flip the default.

    User intent (manual `embed switch`) beats cfg defaults — the cfg
    model is available for use but doesn't take over unless the user
    switches to it.
    """
    # Wipe seed, then register a different default model manually.
    setup_cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
    setup_container = wire(setup_cfg)
    setup_container.db.execute("DELETE FROM embedding_model")
    setup_container.db.execute(
        "INSERT INTO embedding_model "
        "(slug, name, provider, dimension, vec_table, is_default, status, config, created_at) "
        "VALUES ('user-choice', 'user-choice', 'openai-compat', 1024, 'vec_user_choice_1024', "
        "1, 'active', '{}', strftime('s','now'))"
    )
    setup_container.close()

    # wire() with cfg.model='from-cfg' — reconcile registers 'from-cfg'
    # but must leave 'user-choice' as the default.
    cfg = Config(
        data_dir=tmp_path,
        embedder=EmbedderConfig(
            enabled=True,
            provider="openai-compat",
            model="from-cfg",
            dimension=1024,
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
        ),
    )
    container = wire(cfg)
    try:
        defaults = container.db.execute(
            "SELECT slug FROM embedding_model WHERE is_default = 1"
        ).fetchall()
        assert defaults == [("user-choice",)], (
            "user's manual default must not be overridden by reconcile; "
            f"got {defaults}"
        )
        # 'from-cfg' should still exist, just not be default.
        from_cfg = container.db.execute(
            "SELECT is_default FROM embedding_model WHERE slug = 'from-cfg'"
        ).fetchone()
        assert from_cfg is not None, "reconcile should still register 'from-cfg'"
        assert from_cfg[0] == 0
    finally:
        container.close()


def test_wire_enabled_embedder_with_seeded_active_model(tmp_path: Path) -> None:
    """embedder.enabled=True + bge-m3 seed → full embed pipeline constructed.

    Migration 0002 seeds ``bge-m3`` (provider=ollama) so this exercises
    the success path without needing to register a model first. We don't
    call ``ping_embedder()`` here — that hits a live backend and would
    fail when Ollama isn't running locally. Subcommand tests in 6.4 will
    cover the diagnostic flow with FakeEmbedder-style seams.
    """
    cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=True))
    container = wire(cfg)
    try:
        assert container.embed is not None
        assert container.models is not None
        assert container.backfill is not None
        assert container.worker is not None
        assert container.reembed is not None
    finally:
        container.close()


# ---------------------------------------------------------------------------
# Typer app + global flags
# ---------------------------------------------------------------------------


def _register_dummy_command() -> None:
    """Add a no-op command so `app` has at least one subcommand to invoke.

    Typer requires at least one command or `app` prints help and exits
    non-zero. Real subcommands land in Tasks 6.2-6.5; this helper
    registers a probe command for flag-propagation tests only.
    """
    # Idempotent: skip if a previous test in this session already added it.
    existing = {c.name or c.callback.__name__ for c in app.registered_commands}
    if "__probe__" in existing:
        return

    @app.command(name="__probe__", hidden=True)
    def _probe() -> None:
        """No-op command that exists so the callback can fire in tests."""


def test_app_has_no_subcommands_until_subcommand_files_register_them() -> None:
    """6.1 only ships the skeleton — subcommands arrive in 6.2-6.5.

    A bare `app --help` should succeed (exit 0) and print usage text.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


def test_global_flags_parse_via_callback(tmp_path: Path) -> None:
    """--config / --json / --verbose set the module-level flag globals."""
    _register_dummy_command()
    cfg_path = tmp_path / "custom.yaml"
    cfg_path.write_text(f"data_dir: {tmp_path / 'data'}\n")

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg_path), "--json", "--verbose", "__probe__"])
    assert result.exit_code == 0, result.stdout
    assert get_config_path() == cfg_path
    assert is_json_mode() is True
    assert get_verbose() is True


def test_flags_default_when_unset() -> None:
    """No --json / --verbose → both False; --config defaults to None."""
    _register_dummy_command()
    runner = CliRunner()
    result = runner.invoke(app, ["__probe__"])
    assert result.exit_code == 0, result.stdout
    assert is_json_mode() is False
    assert get_verbose() is False
    assert get_config_path() is None
