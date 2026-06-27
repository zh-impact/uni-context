"""SQLite-backed :class:`SchemaMetaImpl` — reads the ``schema_meta`` table.

Ports Go's ``archive/go/internal/adapter/sqlite/schema_meta.go``. The
diagnostic service uses this to surface the migration version without
the CLI reaching into the raw ``*sql.DB`` connection. The query mirrors
the unexported ``readVersion`` in ``migrations_runner.py`` — kept
separate so the doctor path can evolve independently of the migration
runner.

Why a separate class (vs. reading via the migration runner)?
============================================================

The migration runner is one-shot (called once at startup); the doctor
path can be invoked any time later, possibly on a connection the
runner never touched (e.g. a read-only diagnostic connection). A
dedicated ``SchemaMetaImpl`` keeps the read path simple and isolated.
This mirrors Go's split: ``migrations.go:readVersion`` (private to the
runner) vs ``schema_meta.go:Version`` (public API on a struct).
"""

from __future__ import annotations

import sqlite3

from unictx.embed.errors import SchemaMetaNotFound

__all__ = ["SchemaMetaImpl"]


_VERSION_SQL = "SELECT value FROM schema_meta WHERE key='schema_version'"


class SchemaMetaImpl:
    """Reads ``schema_meta.schema_version``.

    Constructed with a :mod:`sqlite3` connection (typically produced by
    :func:`unictx.storage.db.open_db`). The connection MUST have had
    the ``schema_meta`` table created by the migration runner — that
    happens unconditionally on first call to
    :func:`unictx.storage.migrations_runner.migrate`.

    The Go reference (``schema_meta.go:Version``) wraps the
    ``sql.ErrNoRows`` case with "read schema_version" context. Python
    surfaces :class:`SchemaMetaNotFound` so callers can distinguish
    "schema_meta table is empty" from "connection broken".
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def version(self) -> str:
        """Return the ``schema_version`` row value as a string.

        Raises
        ------
        SchemaMetaNotFound
            If no row matches ``key='schema_version'``. Should be
            unreachable on a normally-bootstrapped DB (the migration
            runner seeds the key on first run).
        sqlite3.OperationalError
            If the ``schema_meta`` table doesn't exist (the migration
            runner hasn't been called yet). Surfaces the underlying
            SQLite error verbatim — different failure mode than a
            missing row, and the caller (typically a doctor / startup
            path) wants the raw error.
        """
        row = self._db.execute(_VERSION_SQL).fetchone()
        if row is None:
            raise SchemaMetaNotFound()
        # row_factory (scan_item) passes through raw tuples for
        # non-context_item SELECTs; this query projects a single
        # ``value`` column, so we read positionally.
        return row[0]
