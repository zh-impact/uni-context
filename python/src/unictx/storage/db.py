"""SQLite connection factory with sqlite-vec extension loading.

This module is the foundation of the storage layer: every repository,
migration runner, and searcher in `unictx.storage` is composed on top of
a connection returned by :func:`open_db`. Callers compose:

    db = open_db(...)
    migrate(db)            # Task 2.2 — applied separately, not by open_db
    repo = ContextRepo(db)

Differences from Go
===================

The Go reference is ``archive/go/internal/adapter/sqlite/db.go:Open``.
The Python port diverges in three intentional ways:

1. **No auto-migrate.** Go's ``Open`` calls ``Migrate(db)`` after opening.
   The Python port does **not** — :func:`open_db` only opens the connection.
   ``migrate(db)`` (Task 2.2) is a separate function the caller composes
   explicitly. Rationale: separation of concerns. The connection factory
   must be testable in isolation, and Task 2.2's tests need to call
   ``migrate`` against a bare ``:memory:`` connection without going through
   any of the connection-factory code paths.

2. **No file-permission tightening (yet).** Go's ``tightenDBFilePermissions``
   best-effort chmod to 0600 is deferred — Plan 2c persists API keys inside
   the ``embedding_model.config`` JSON column, so a group/world-readable DB
   would leak them. DEFERRED: file permission tightening (Go's
   ``tightenDBFilePermissions`` equivalent) — tracked for a later phase.

3. **Autocommit isolation_level.** Go's ``database/sql`` does not wrap DDL
   in implicit transactions; Python's :mod:`sqlite3` does by default. We
   set ``isolation_level=None`` (autocommit) so the migration runner
   (Task 2.2) can drive explicit ``BEGIN``/``COMMIT`` around each migration
   step. Trade-off: every DML statement outside an explicit transaction is
   auto-committed — callers that need atomicity across multiple statements
   must wrap them in ``with db:`` or issue ``BEGIN`` themselves. This
   matches Go's behavior and is what the migration runner requires.

DSN shape
=========

The DSN mirrors the Go reference:

    file:<path>?_journal_mode=WAL&_synchronous=NORMAL&_busy_timeout=5000&\
_foreign_keys=on&_temp_store=MEMORY[&mode=ro]

WAL on ``:memory:`` is silently ignored by SQLite (in-memory DBs always
use MEMORY journal) — we pass the same DSN shape anyway for symmetry.
File-based tests exercise WAL; in-memory tests do not, which is fine.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from unictx.storage.row_factory import scan_item

__all__ = ["open_db"]


def open_db(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection with sqlite-vec loaded and pragmas applied.

    Parameters
    ----------
    path:
        File path or the literal string ``":memory:"``. ``":memory:"`` is
        passed straight to :func:`sqlite3.connect` (which has special
        handling) — it is **not** URI-encoded.
    read_only:
        When ``True`` and *path* is a file, open in read-only mode via the
        ``mode=ro`` URI parameter. Ignored for ``":memory:"`` (in-memory
        databases are always read-write and never persist).

    Returns
    -------
    sqlite3.Connection
        A connection with:

        * ``isolation_level=None`` (autocommit — see module docstring).
        * Foreign keys, WAL, NORMAL synchronous, 5s busy timeout, and
          MEMORY temp store enabled via the DSN.
        * The ``vec0`` module loaded (``sqlite_vec.load``).

    Raises
    ------
    sqlite3.OperationalError
        If the database file cannot be opened (e.g. parent directory does
        not exist) or the extension fails to load.
    """
    # ":memory:" must be passed literally — sqlite3.connect has special
    # handling for it, and URI-encoding it (file::memory:?...) would open
    # a *different* in-memory database on every connect within a process.
    #
    # For file-backed DBs we still use the URI form (file:<path>?mode=ro)
    # because read-only mode is only expressible via the `mode` URI param.
    # NOTE: Python's stdlib sqlite3 only honors a small subset of URI
    # query parameters (`mode`, `cache`) — it does NOT honor the
    # `_journal_mode=...` / `_foreign_keys=...` underscore-prefixed pragmas
    # that some drivers (e.g. mattn/go-sqlite3, SQLAlchemy) translate.
    # The Go reference relies on that translation; we instead apply each
    # PRAGMA explicitly via PRAGMA statements after connect.
    if path == ":memory:":
        # WAL is silently ignored for in-memory DBs; we still set the
        # isolation_level and pragmas the same way as for file-backed DBs.
        conn = sqlite3.connect(":memory:", isolation_level=None)
    else:
        path_str = str(Path(path))
        dsn = f"file:{path_str}"
        if read_only:
            dsn += "?mode=ro"
        conn = sqlite3.connect(dsn, uri=True, isolation_level=None)

    # Apply PRAGMAs the Go DSN expressed as _journal_mode etc. Read-only
    # connections accept all of these (they're connection-scoped, not
    # file-mutating); journal_mode is the one exception — on a read-only
    # connection it's a no-op, which is correct.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")

    # Load the sqlite-vec extension. sqlite_vec.load() handles path
    # resolution across platforms; we just have to enable extension
    # loading on the connection first, then lock it back down after.
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        # Defensive: disable further extension loading so untrusted SQL
        # can't pull in arbitrary shared objects via load_extension().
        conn.enable_load_extension(False)

    # Row factory: every SELECT against context_item returns a ContextItem
    # directly (see storage/row_factory.py). Registered here so callers
    # never need to set it themselves; tests that open a bare :memory:
    # via this function get the same row mapping as production. The
    # factory sniffs column names — SELECTs whose column set doesn't
    # match context_item's (e.g. joins over context_fts/vec0 tables,
    # reads from embedding_model/schema_meta) pass through as raw tuples
    # with no opt-in required. See row_factory.py:scan_item for the
    # sniffing contract.
    conn.row_factory = scan_item

    # Ping — SQLite doesn't surface open errors until first use. SELECT 1
    # is the cheapest way to force that surface.
    conn.execute("SELECT 1").fetchone()

    return conn
