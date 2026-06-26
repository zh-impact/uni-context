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
    or use a ``migrated_db`` fixture (added in Task 2.2 once migrate exists).

    Uses ``:memory:`` rather than ``tmp_path`` — in-memory DBs don't need a
    filesystem path, are faster, and are isolated per-test by construction.
    """
    from unictx.storage.db import open_db

    db = open_db(":memory:")
    yield db
    db.close()
