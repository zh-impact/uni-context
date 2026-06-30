"""Shared pytest fixtures for the uni-context Python test suite.

Fixtures defined here are auto-available to every test under tests/.
Phase-specific conftest.py files (e.g. tests/storage/conftest.py)
live next to their tests and add fixtures only those tests need.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from tests._fakes.canned_filestore import CannedFileStore
from tests._fakes.fake_embedder import FakeEmbedder
from tests._fakes.fake_repo import FakeContextRepo


@pytest.fixture
def fake_repo() -> FakeContextRepo:
    """Empty in-memory ContextRepo. Mutate .items directly to seed."""
    return FakeContextRepo()


@pytest.fixture
def canned_fs() -> CannedFileStore:
    """Empty CannedFileStore. Seed via .data[uri] = bytes."""
    return CannedFileStore()


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    """1024-dim deterministic embedder. Override .model_info to customize."""
    return FakeEmbedder(dimension=1024)


@pytest.fixture
def tmp_db() -> Iterator[sqlite3.Connection]:
    """Fresh :memory: SQLite with sqlite-vec loaded; no migrations applied.

    Storage tests that need schema call ``migrate(db)`` (Task 2.2) themselves,
    or use the ``migrated_db`` fixture below.

    Uses ``:memory:`` rather than ``tmp_path`` — in-memory DBs don't need a
    filesystem path, are faster, and are isolated per-test by construction.
    """
    from unictx.storage.db import open_db

    db = open_db(":memory:")
    yield db
    db.close()


@pytest.fixture
def migrated_db() -> Iterator[sqlite3.Connection]:
    """Fresh :memory: DB with all migrations applied (schema_version='5').

    Ready for storage-impl tests (Task 2.3+): the connection already has
    ``row_factory = scan_item`` set by :func:`open_db`, and the schema is
    fully migrated. Each test gets its own isolated in-memory DB.
    """
    from unictx.storage.db import open_db
    from unictx.storage.migrations_runner import migrate

    db = open_db(":memory:")
    migrate(db)
    yield db
    db.close()
