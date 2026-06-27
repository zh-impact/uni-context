"""SQLite-backed :class:`EmbeddingRepo` implementation (status rows only).

Ports Go's ``archive/go/internal/adapter/sqlite/embedding_repo.go``.
This is the concrete storage-side impl of the
:class:`unictx.embed.embedding_repo.EmbeddingRepo` Protocol — STATUS
ONLY, no vector methods. Vector writes live in
:class:`unictx.storage.vectorstore_impl.VectorStoreImpl`; the two
tables are separate (``context_embedding`` vs ``vec_<slug>_<dim>``).

Method-by-method mapping to Go
==============================

* ``upsert_status`` — Go ``UpsertStatus``. Single ``INSERT ... ON
  CONFLICT DO UPDATE`` statement (no transaction wrapper needed; one
  statement auto-commits atomically under SQLite). On conflict,
  ``attempts`` is incremented by 1 and ``embedded_at``/``status``/
  ``error``/``last_error`` are overwritten from the ``excluded``
  pseudo-row. Both ``error`` (original 0002 column) and ``last_error``
  (0003 addition) bind to the same ``err_str`` for backward-compat —
  mirrors Go verbatim.
* ``get_status`` — Go ``GetStatus``. ``fetchone() is None`` replaces
  Go's ``errors.Is(err, sql.ErrNoRows)`` → raise
  :class:`unictx.embed.errors.StatusNotFound`.
* ``list_failed`` — Go ``ListFailed``. ``ORDER BY embedded_at ASC``
  (oldest failures first — they've waited longest). ``limit <= 0``
  defaults to 100.
* ``list_for_item`` — Go ``ListForItem``. ``ORDER BY model_slug ASC``.
  Empty list (not None) if no rows.

Row consumption
===============

The connection's ``row_factory`` is
:func:`unictx.storage.row_factory.scan_item`, which inspects column
names and only returns :class:`ContextItem` for ``context_item``-shaped
SELECTs. Our SELECTs project ``item_id, model_slug, status, error,
last_error, attempts, embedded_at`` — none of which match the required
``{id, scope, kind, source, visibility, version}`` set, so ``scan_item``
passes the rows through as raw tuples. We consume them positionally
with ``row[0]..row[6]``.

NULL handling
=============

Go uses ``sql.NullString`` for ``error`` and ``last_error``. Python's
:mod:`sqlite3` surfaces SQL NULL as ``None``; we coalesce to ``""`` to
match :class:`EmbeddingStatus`'s ``str`` field type. ``embedded_at``
is an INTEGER unix timestamp; we pass it through directly without
conversion (the dataclass stores ``int``, not ``datetime.datetime``).

Deviation from the brief
========================

The task brief (``.superpowers/sdd/task-2.6-brief.md:30-31``) says
``list_failed`` uses ``ORDER BY embedded_at DESC``. **This is a typo**:
Go source (``embedding_repo.go:82``) AND the Phase 1 Protocol
docstring (``embedding_repo.py:65``) both say ``ASC`` (oldest failures
first — they've waited longest). We follow Go + Protocol.

The Protocol signature for ``upsert_status`` is
``(item_id, model_slug, status, err_str)`` (no default). The brief
specifies ``err_str=""`` as the call-site default. Defaults are
orthogonal to Protocol structural matching (a Protocol only describes
the call shape, not the param defaults), so we keep the default here
without redefining the Protocol.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from unictx.embed.embedding_repo import EmbeddingStatus
from unictx.embed.errors import StatusNotFound

__all__ = ["EmbeddingRepoImpl"]


# ---------------------------------------------------------------------------
# SQL constants — copied verbatim from Go's embedding_repo.go to keep the
# two ports reviewable side-by-side. Column order MUST match the row unpack
# in _scan_status_row below.
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO context_embedding
    (item_id, model_slug, embedded_at, status, error, last_error, attempts)
VALUES (?, ?, ?, ?, ?, ?, 1)
ON CONFLICT(item_id, model_slug) DO UPDATE SET
    embedded_at = excluded.embedded_at,
    status      = excluded.status,
    error       = excluded.error,
    last_error  = excluded.last_error,
    attempts    = context_embedding.attempts + 1
"""

_GET_STATUS_SQL = """
SELECT item_id, model_slug, status, error, last_error, attempts, embedded_at
FROM context_embedding
WHERE item_id = ? AND model_slug = ?
"""

_LIST_FAILED_SQL = """
SELECT item_id, model_slug, status, error, last_error, attempts, embedded_at
FROM context_embedding
WHERE status = 'failed'
ORDER BY embedded_at ASC
LIMIT ?
"""

_LIST_FOR_ITEM_SQL = """
SELECT item_id, model_slug, status, error, last_error, attempts, embedded_at
FROM context_embedding
WHERE item_id = ?
ORDER BY model_slug ASC
"""

# Default cap for list_failed when caller passes limit <= 0. Mirrors Go's
# embedding_repo.go:88. Not a clamp — values > 100 are honored verbatim.
_DEFAULT_LIST_FAILED_LIMIT = 100


class EmbeddingRepoImpl:
    """Status-only :class:`EmbeddingRepo` backed by a SQLite connection.

    The connection MUST have all migrations applied (Task 2.2) and SHOULD
    come from :func:`unictx.storage.db.open_db`, which sets
    ``PRAGMA foreign_keys = ON`` so that deleting a ``context_item`` row
    cascades to ``context_embedding`` (migration 0002 + 0004 set up the
    FK + ON DELETE CASCADE).
    """

    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def upsert_status(
        self,
        item_id: str,
        model_slug: str,
        status: str,
        err_str: str = "",
    ) -> None:
        """Insert or update the status row for (item_id, model_slug).

        On conflict: ``attempts`` is incremented by 1 (fresh INSERT
        starts at 1), ``embedded_at`` is set to now (UTC unix ts),
        ``status``/``error``/``last_error`` are overwritten from the
        bound params. Both ``error`` and ``last_error`` bind to
        ``err_str`` for backward-compat (the original 0002 ``error``
        column and the 0003 ``last_error`` column carry the same text;
        ``last_error`` is the authoritative "most recent" field).
        """
        now = int(datetime.now(UTC).timestamp())
        self._db.execute(
            _UPSERT_SQL,
            (item_id, model_slug, now, status, err_str, err_str),
        )

    def get_status(self, item_id: str, model_slug: str) -> EmbeddingStatus:
        """Return the row for (item_id, model_slug).

        Raises :class:`StatusNotFound` if no row matches.
        """
        row = self._db.execute(_GET_STATUS_SQL, (item_id, model_slug)).fetchone()
        if row is None:
            raise StatusNotFound(item_id, model_slug)
        return _scan_status_row(row)

    def list_failed(self, limit: int) -> list[EmbeddingStatus]:
        """Up to ``limit`` rows with ``status='failed'``.

        Ordered by ``embedded_at ASC`` (oldest failures first — they've
        waited longest). ``limit <= 0`` defaults to 100 (mirrors Go).
        Empty list (not None) if no rows.
        """
        if limit <= 0:
            limit = _DEFAULT_LIST_FAILED_LIMIT
        rows = self._db.execute(_LIST_FAILED_SQL, (limit,)).fetchall()
        return [_scan_status_row(r) for r in rows]

    def list_for_item(self, item_id: str) -> list[EmbeddingStatus]:
        """All status rows for the item, ordered by ``model_slug ASC``.

        Empty list (not None) if no rows. Used by ``embed status <id>``
        CLI to show per-model migration state.
        """
        rows = self._db.execute(_LIST_FOR_ITEM_SQL, (item_id,)).fetchall()
        return [_scan_status_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scan_status_row(row: tuple) -> EmbeddingStatus:
    """Build an :class:`EmbeddingStatus` from a raw tuple.

    Column order matches the SELECT statements above (and Go's
    ``rows.Scan`` order):

    0 item_id, 1 model_slug, 2 status, 3 error, 4 last_error,
    5 attempts, 6 embedded_at.

    ``error`` and ``last_error`` may be SQL NULL → coalesce to ``""``
    (mirrors Go's ``sql.NullString.String`` zero value). ``embedded_at``
    is an INTEGER unix ts — passed through directly without conversion
    (the dataclass stores ``int``, not ``datetime``).
    """
    return EmbeddingStatus(
        item_id=row[0],
        model_slug=row[1],
        status=row[2],
        error=row[3] or "",
        last_error=row[4] or "",
        attempts=row[5],
        embedded_at=row[6],
    )
