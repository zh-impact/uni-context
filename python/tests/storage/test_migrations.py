"""Tests for :mod:`unictx.storage.migrations_runner`.

Covers: full migration applies the latest version; idempotency; expected
tables present; version parser; FTS5 hint detection; non-FTS5 passthrough;
and a skip-guarded smoke test against the Go-written DB if present on
this machine.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from unictx.storage.db import open_db
from unictx.storage.migrations_runner import (
    _exec_migration,
    _version_from_name,
    _wrap_migration_err,
    migrate,
)


def test_migrate_applies_all() -> None:
    """After ``migrate``, ``schema_meta.schema_version`` must be the latest.

    The version tracks the highest migration applied; bump this constant
    whenever a new migration file is added.
    """
    db = open_db(":memory:")
    try:
        migrate(db)
        row = db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    finally:
        db.close()
    assert row is not None
    assert row[0] == "5"


def test_migrate_idempotent() -> None:
    """Calling ``migrate`` twice is a no-op the second time."""
    db = open_db(":memory:")
    try:
        migrate(db)
        # Second call: every migration is skipped because v <= current.
        migrate(db)
        (version,) = db.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    finally:
        db.close()
    assert version == "5"


def test_migrate_creates_expected_tables() -> None:
    """The four key tables from the migrations exist after ``migrate``."""
    db = open_db(":memory:")
    try:
        migrate(db)
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        db.close()
    names = {r[0] for r in rows}
    # The four key tables called out in the task brief.
    assert "context_item" in names
    assert "context_fts" in names
    assert "context_embedding" in names
    assert "embedding_model" in names


def test_version_from_name_parses_leading_digits() -> None:
    """The parser extracts the leading NNNN from migration filenames."""
    assert _version_from_name("0001_init.sql") == 1
    assert _version_from_name("0004_embedding_model_slug_cascade.sql") == 4


def test_version_from_name_returns_zero_on_no_match() -> None:
    """Files that don't match the ``\\d+_.*\\.sql`` pattern yield 0.

    Mirrors Go's fallback behavior — such files would be skipped (v=0
    is always <= current after the first migration runs).
    """
    assert _version_from_name("README.md") == 0
    assert _version_from_name("__init__.py") == 0


def test_wrap_migration_err_fts5_hint() -> None:
    """A 'no such module: fts5' error surfaces the Python FTS5 hint."""
    original = sqlite3.OperationalError("no such module: fts5")
    wrapped = _wrap_migration_err("0001_init.sql", original)
    msg = str(wrapped)
    # The filename is attached.
    assert "0001_init.sql" in msg
    # The hint wording is the Python-context one (not the Go -tags hint).
    assert "FTS5" in msg
    assert "-tags sqlite_fts5" not in msg
    # And the original error is still present for diagnosis.
    assert "no such module: fts5" in msg


def test_wrap_migration_err_passthrough() -> None:
    """Non-FTS5 errors are wrapped with the filename and the message survives."""
    original = sqlite3.OperationalError("syntax error near CREATE")
    wrapped = _wrap_migration_err("0002_embeddings.sql", original)
    assert isinstance(wrapped, RuntimeError)
    msg = str(wrapped)
    assert "0002_embeddings.sql" in msg
    assert "syntax error near CREATE" in msg


def test_exec_migration_chains_cause_on_failure() -> None:
    """A failed ``_exec_migration`` raises with the underlying error chained.

    Verifies the ``raise _wrap_migration_err(fname, exc) from exc`` pattern
    in :func:`_exec_migration`: the original sqlite error must be reachable
    via ``__cause__`` on the wrapped RuntimeError. Mirrors Go's ``%w``
    wrapping — callers that special-case specific sqlite errors can still
    introspect the root cause.
    """
    db = open_db(":memory:")
    try:
        with pytest.raises(RuntimeError) as exc_info:
            _exec_migration(db, "broken.sql", "SELECT * FROM no_such_table_xyz")
        wrapped = exc_info.value
        # The wrapped exception carries the filename.
        assert "broken.sql" in str(wrapped)
        # The original error is reachable via __cause__.
        cause = wrapped.__cause__
        assert isinstance(cause, sqlite3.OperationalError)
        assert "no such table" in str(cause)
        # The transaction was rolled back — no schema changes leaked.
        # (We never created any in this body, but verify no transaction
        # is left open by checking we can begin another.)
        db.execute("BEGIN")
        db.execute("COMMIT")
    finally:
        db.close()


_GO_DB_PATH = Path.home() / ".local" / "share" / "unictx" / "unictx.db"


@pytest.mark.skipif(
    not _GO_DB_PATH.exists(),
    reason="no Go DB on this machine (developer smoke test, not CI)",
)
def test_migrate_go_db_advances_to_latest() -> None:
    """``migrate`` brings an existing Go DB (v=4) up to the latest version.

    The Go DB lives at ``~/.local/share/unictx/unictx.db`` and was last
    written by the Go build at v=4. After Python migration 0005, the
    runner advances it to v=5 (additive — only creates access_grant).
    CI machines don't have it; this only runs on developer machines
    where it exists. Read-write is fine: 0005 is a pure CREATE TABLE.
    """
    db = open_db(_GO_DB_PATH)
    try:
        migrate(db)
        (version,) = db.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    finally:
        db.close()
    assert version == "5"
