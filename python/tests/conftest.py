"""Shared pytest fixtures for the uni-context Python test suite.

Fixtures defined here are auto-available to every test under tests/.
Phase-specific conftest.py files (e.g. tests/storage/conftest.py)
live next to their tests and add fixtures only those tests need.

The tmp_db fixture (in-memory SQLite via unictx.storage.db.open_db)
is intentionally NOT here — storage.db doesn't exist until Phase 2
Task 2.1. That task will add tmp_db to this file (see task-1.6 brief
note: "defer tmp_db fixture to Phase 2 Task 2.1").
"""

from __future__ import annotations

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
