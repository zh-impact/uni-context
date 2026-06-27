"""Tests for unictx.storage.db.open_db."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from unictx.storage.db import open_db


def test_open_memory_loads_vec_version() -> None:
    """open_db(':memory:') must load the sqlite-vec extension."""
    db = open_db(":memory:")
    try:
        (version,) = db.execute("SELECT vec_version()").fetchone()
    finally:
        db.close()
    assert isinstance(version, str)
    assert version != ""


def test_open_memory_foreign_keys_pragma_on() -> None:
    """The _foreign_keys=on DSN parameter must take effect."""
    db = open_db(":memory:")
    try:
        (fk,) = db.execute("PRAGMA foreign_keys").fetchone()
    finally:
        db.close()
    assert fk == 1


def test_open_memory_isolation_level_autocommit() -> None:
    """isolation_level=None so the migration runner can drive transactions."""
    db = open_db(":memory:")
    try:
        assert db.isolation_level is None
    finally:
        db.close()


def test_open_file_backed_wal(tmp_path: Path) -> None:
    """A file-backed DB persists rows across reopens and uses WAL journal."""
    db_file = tmp_path / "test.db"

    db = open_db(db_file)
    try:
        db.execute("CREATE TABLE t (x INTEGER)")
        db.execute("INSERT INTO t VALUES (1)")
    finally:
        db.close()

    # Reopen and verify the row persisted.
    db2 = open_db(db_file)
    try:
        (val,) = db2.execute("SELECT x FROM t").fetchone()
        assert val == 1
        (mode,) = db2.execute("PRAGMA journal_mode").fetchone()
        assert mode == "wal"
    finally:
        db2.close()


def test_open_read_only_existing_file(tmp_path: Path) -> None:
    """read_only=True opens an existing DB read-only; INSERT must fail."""
    db_file = tmp_path / "test.db"

    # Create with a row in read-write mode.
    db = open_db(db_file)
    try:
        db.execute("CREATE TABLE t (x INTEGER)")
        db.execute("INSERT INTO t VALUES (42)")
    finally:
        db.close()

    # Reopen read-only.
    ro = open_db(db_file, read_only=True)
    try:
        (val,) = ro.execute("SELECT x FROM t").fetchone()
        assert val == 42
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            ro.execute("INSERT INTO t VALUES (100)")
    finally:
        ro.close()


def test_open_nonexistent_directory_raises(tmp_path: Path) -> None:
    """Opening a DB in a nonexistent directory surfaces the error."""
    bad = tmp_path / "no_such_dir" / "db.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        open_db(bad)


_GO_DB_PATH = Path.home() / ".local" / "share" / "unictx" / "unictx.db"


@pytest.mark.skipif(
    not _GO_DB_PATH.exists(),
    reason="no Go DB on this machine (developer smoke test, not CI)",
)
def test_open_go_written_db_read_only() -> None:
    """Manual smoke: read schema_meta from a Go-written DB, read-only.

    The Go DB lives at ~/.local/share/unictx/unictx.db. CI machines don't
    have it — this test only runs on developer machines where it exists.
    """
    db = open_db(_GO_DB_PATH, read_only=True)
    try:
        row = db.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    finally:
        db.close()
    assert row is not None
    (version,) = row
    assert isinstance(version, str)
    assert version != ""
