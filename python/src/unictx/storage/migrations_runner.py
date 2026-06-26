"""SQLite migration runner — ports Go's ``migrations.go``.

This is the second half of storage bootstrap (Task 2.1 ``open_db`` is the
first). Callers compose::

    db = open_db(...)
    migrate(db)               # this module — separate, not called by open_db
    repo = ContextRepo(db)

The Go reference is ``archive/go/internal/adapter/sqlite/migrations.go``.

Transaction handling
====================

Go wraps each migration file in a single ``BeginTx``/``Commit`` transaction
and ships the whole file body to ``tx.Exec`` as one call (the SQL driver
splits statements). The Python port must do the same per-file transaction,
but :mod:`sqlite3` has a wrinkle: ``executescript()`` issues an implicit
``COMMIT`` *before* running the script, so wrapping an ``executescript``
call inside an explicit ``BEGIN``/``COMMIT`` does not work — the implicit
COMMIT closes the BEGIN immediately.

We use **Option B** from the task brief: split the file body into
statements and execute each one inside an explicit ``BEGIN``/``COMMIT``
block. This matches Go's semantics (one tx per file, atomic across all
statements in the file) and works with ``isolation_level=None`` (which
:func:`unictx.storage.db.open_db` sets).

Statement splitter assumption
-----------------------------
The splitter walks the body character-by-character and tracks three
kinds of context that suppress ``;`` splitting:

* ``--`` line comments (extend to end of line) — the shipped migrations
  contain ``;`` inside ``--`` comments (e.g. ``-- true; Plan 2c...``);
  these must NOT terminate a statement.
* ``'...'`` string literals — grepping confirms the shipped migrations
  contain no ``;`` inside string literals, but we honor them anyway for
  robustness (a future migration might add one).
* ``BEGIN ... END`` trigger bodies — the inner ``;`` between
  ``BEGIN`` and the closing ``END;`` is part of the trigger definition,
  not a statement terminator.

Two more assumptions that are checked against the shipped migrations:

1. **No block comments** (``/* ... */``). None of the four files contain
   ``/*`` (verified by grep). If a future migration adds one, the
   splitter needs upgrading.

2. **No standalone ``BEGIN TRANSACTION`` statements.** The migrations
   deliberately omit them (the runner wraps each file in its own tx;
   SQLite rejects nested BEGIN). The only ``BEGIN`` token in the SQL is
   the ``CREATE TRIGGER ... BEGIN ... END`` form. So the splitter treats
   every ``BEGIN`` keyword as opening a trigger body and every ``END``
   as closing it.

Why not just use ``executescript`` (Option A)?  The task brief offers
Option A as "simple, accepted" — but Option B is more faithful to Go's
semantics (Go's per-file transaction is preserved) and costs ~10 lines of
extra code. Since the splitter assumption is verifiable for the current
file set, Option B is the right trade-off.

FTS5 hint
=========

Go's ``wrapMigrationErr`` emits a hint pointing at ``-tags sqlite_fts5``
because Go's ``mattn/go-sqlite3`` builds FTS5 out by default and the tag
must be passed to enable it. Python's stdlib :mod:`sqlite3` is the
opposite: FTS5 is **built in by default** on CPython distributions that
bundle a modern SQLite (which is essentially all of them — the official
python.org builds, Homebrew Python, and most Linux distros). So the Python
hint changes: instead of "rebuild with -tags sqlite_fts5", we say "your
Python's bundled SQLite was built without FTS5; reinstall Python or use
a distribution that ships FTS5-enabled SQLite". The wrap-and-detect
pattern itself is identical to Go.
"""

from __future__ import annotations

import re
import sqlite3
from importlib.resources import files
from pathlib import Path

__all__ = ["migrate"]

_FTS5_HINT = (
    "SQLite was built without FTS5 — your Python's bundled sqlite3 module "
    "lacks FTS5 support (Python's stdlib sqlite3 ships with FTS5 enabled "
    "by default; if you hit this, reinstall Python or use a distribution "
    "that ships an FTS5-enabled SQLite). See CLAUDE.md / Makefile."
)

_VERSION_RE = re.compile(r"(\d+)_.*\.sql$")


def migrate(db: sqlite3.Connection) -> None:
    """Apply all pending migrations in version order.

    Idempotent: migrations whose version is ``<=`` the current
    ``schema_meta.schema_version`` are skipped. The current version is
    bumped inside each migration file (each SQL file ends with
    ``UPDATE schema_meta SET value = '<N>'``).

    Raises
    ------
    Exception
        Wraps any per-migration error with the filename via
        :func:`_wrap_migration_err`. If the error indicates FTS5 is
        missing, the wrapped message carries an actionable Python-context
        hint.
    """
    _ensure_schema_meta(db)
    current = _read_version(db)

    for path in _sorted_migration_files():
        v = _version_from_name(path.name)
        if v <= current:
            continue
        body = path.read_text()
        _exec_migration(db, path.name, body)


def _ensure_schema_meta(db: sqlite3.Connection) -> None:
    """Create ``schema_meta`` if missing and seed ``schema_version='0'``.

    Idempotent. Matches Go's ``ensureSchemaMeta``.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    db.execute("INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '0')")


def _read_version(db: sqlite3.Connection) -> int:
    """Read the integer ``schema_version`` from ``schema_meta``."""
    (s,) = db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    return int(s)


def _sorted_migration_files() -> list[Path]:
    """List embedded ``migrations/*.sql`` files sorted by filename.

    Uses :mod:`importlib.resources` so the SQL files are locatable both
    when ``unictx`` is ``pip``-installed (files live inside the installed
    package) and when run from source (files live in the working tree).
    """
    migrations_dir = files("unictx.storage").joinpath("migrations")
    # ``files()`` returns a Traversable. ``iterdir`` works on both the
    # installed-zip and the source-tree cases. Sort by name so the
    # leading-digit prefix gives us application order.
    paths = [Path(str(p)) for p in migrations_dir.iterdir() if p.is_file()]
    return sorted(paths, key=lambda p: p.name)


def _version_from_name(name: str) -> int:
    """Parse the leading ``NNNN`` from a migration filename.

    Returns ``0`` if the filename doesn't match ``\\d+_.*\\.sql`` —
    matches Go's ``versionFromName`` fallback behavior.
    """
    m = _VERSION_RE.search(name)
    if m is None:
        return 0
    return int(m.group(1))


def _exec_migration(db: sqlite3.Connection, fname: str, body: str) -> None:
    """Apply one migration's body in a single transaction.

    See the module docstring for why we split statements and use explicit
    BEGIN/COMMIT instead of ``executescript``. The connection must be in
    autocommit mode (``isolation_level=None``), which :func:`open_db`
    guarantees.
    """
    db.execute("BEGIN")
    try:
        for stmt in _split_statements(body):
            db.execute(stmt)
    except Exception as exc:
        db.execute("ROLLBACK")
        raise _wrap_migration_err(fname, exc) from exc
    db.execute("COMMIT")


def _split_statements(body: str) -> list[str]:
    """Split a SQL body into individual statements on bare ``;``.

    Respects three suppressors (see module docstring): line comments
    (``--``), string literals (``'...'``), and ``BEGIN ... END`` trigger
    bodies. Does NOT handle block comments (``/* */``) — the shipped
    migrations contain none.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(body)
    trigger_depth = 0  # nesting level of BEGIN ... END trigger bodies
    while i < n:
        ch = body[i]

        # Line comment: -- through end of line. Don't emit anything special;
        # we just want to suppress ; within it. Easiest: copy the whole
        # comment line into the buffer verbatim.
        if ch == "-" and i + 1 < n and body[i + 1] == "-":
            end = body.find("\n", i)
            if end == -1:
                buf.append(body[i:])
                i = n
            else:
                buf.append(body[i : end + 1])
                i = end + 1
            continue

        # String literal: '...'. Honor '' as an escaped quote inside.
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(body[i])
                if body[i] == "'":
                    # Check for escaped '' (SQL standard). Consume both
                    # quotes as part of the same literal.
                    if i + 1 < n and body[i + 1] == "'":
                        buf.append(body[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Track BEGIN/END trigger-body depth so ; inside a trigger body
        # does NOT split. Only meaningful at trigger boundaries — every
        # BEGIN in the shipped files opens a trigger body, every END
        # closes one.
        if ch.isalpha() or ch == "_":
            # Read the next identifier.
            j = i
            while j < n and (body[j].isalnum() or body[j] == "_"):
                j += 1
            word = body[i:j].lower()
            # Identifier must be preceded by whitespace or start of buf
            # (after stripping) to count as a keyword. We approximate by
            # checking the last char in buf is whitespace or empty.
            prev_ok = (not buf) or buf[-1].isspace() or buf[-1] == ";"
            if word == "begin" and prev_ok:
                trigger_depth += 1
            elif word == "end" and prev_ok and trigger_depth > 0:
                trigger_depth -= 1
            buf.append(body[i:j])
            i = j
            continue

        buf.append(ch)
        if ch == ";" and trigger_depth == 0:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _wrap_migration_err(fname: str, err: Exception) -> Exception:
    """Attach the migration filename to a per-statement error.

    Mirrors Go's ``wrapMigrationErr``: if the error message indicates
    FTS5 is missing, the wrapped message carries the Python-context hint
    (see :data:`_FTS5_HINT`). Otherwise the message just prepends the
    filename. The original error is chained via ``raise ... from err`` in
    :func:`_exec_migration`, preserving ``__cause__`` for callers that
    want to introspect (mirrors Go's ``%w`` wrapping).
    """
    if "no such module: fts5" in str(err):
        return RuntimeError(f"exec migration {fname}: {_FTS5_HINT}; underlying error: {err}")
    return RuntimeError(f"exec migration {fname}: {err}")
