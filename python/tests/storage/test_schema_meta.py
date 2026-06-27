"""Tests for unictx.storage.schema_meta.SchemaMetaImpl.

Ports Go's ``schema_meta_test.go`` (the file is small — just a happy
path plus a missing-row path).

The fixture ``migrated_db`` (tests/conftest.py) yields a fresh
``:memory:`` connection with all migrations applied. The migration
runner seeds ``schema_meta.schema_version='4'`` once migration 0004
completes, so ``SchemaMetaImpl.version()`` should return ``"4"``.
"""

from __future__ import annotations

import sqlite3

import pytest

from unictx.embed.errors import SchemaMetaNotFound
from unictx.storage.schema_meta import SchemaMetaImpl


class TestVersion:
    def test_version_returns_value_written_by_migrations(
        self,
        migrated_db: sqlite3.Connection,
    ) -> None:
        """``version()`` returns the row the migrations runner seeded."""
        # The latest migration is 0004 → '4'.
        assert SchemaMetaImpl(migrated_db).version() == "4"

    def test_version_missing_row_raises_schema_meta_not_found(
        self,
        migrated_db: sqlite3.Connection,
    ) -> None:
        """If schema_version row is absent, raise SchemaMetaNotFound."""
        migrated_db.execute("DELETE FROM schema_meta WHERE key='schema_version'")
        with pytest.raises(SchemaMetaNotFound):
            SchemaMetaImpl(migrated_db).version()
