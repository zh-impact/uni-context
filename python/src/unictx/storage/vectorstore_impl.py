"""SQLite-backed :class:`VectorStore` implementation using sqlite-vec.

Ports Go's ``archive/go/internal/adapter/sqlite/vectorstore.go``. This
is the concrete storage-side impl of the
:class:`unictx.search.vectorstore.VectorStore` Protocol — vector
writes (and KNN reads) against the per-model vec0 virtual table live
here.

vec0 UPSERT idiom
=================

vec0 virtual tables do **not** support ``INSERT OR REPLACE`` on their
TEXT PK (the underlying sqlite-vec API errors with
``UNIQUE constraint failed: <table>.primary key``). The UPSERT
therefore is ``DELETE`` then ``INSERT`` inside an explicit
``BEGIN``/``COMMIT`` transaction (mirrors Go's ``Put`` verbatim). The
delete is a no-op if the key is new; the insert is the actual write.
Wrapped in a tx so a partial failure leaves the table unchanged.

KNN query shape
===============

vec0's KNN syntax requires a ``MATCH`` predicate against the embedding
column and the ``k = ?`` parameter (NOT ``LIMIT ?`` — ``LIMIT`` is
ignored by vec0's KNN machinery; ``k`` is the K parameter). Without
``MATCH`` the query degrades to a scan and ``distance`` is not a
valid column, surfacing as ``datatype mismatch``.

Scope/kind filters are pushed down to the same WHERE via JOIN
``context_item`` — there is no post-filter inside this module, so
``VectorStoreImpl.search`` honors ``q.limit`` verbatim (after
``clamp_limit``). Over-fetch is the responsibility of the service
layer (Phase 5 ``searchHybrid``), which passes ``Limit=limit*3`` from
orchestration; this module MUST NOT multiply again (the previous
Go bug at commit 4d26cea did ``fetchN = q.Limit * 3``, making the
effective ``k = limit*9`` at the service layer for default
limit=20 — 180 KNN rows + 180 ``repo.get`` calls).

Distance → score conversion
===========================

Cosine distance is in ``[0, 2]`` (0=identical, 2=opposite). We convert
to similarity in ``[0, 1]`` via ``score = 1 - distance / 2`` so higher
= better match (matches Go's :data:`Score` sign convention; this is NOT
a sign flip — it's a distance/similarity conversion). Both ``distance``
and ``score`` are returned on :class:`VectorHit`; different callers
want different signals.

Row consumption
===============

The connection's ``row_factory`` is :func:`unictx.storage.row_factory.scan_item`,
which inspects column names and only returns :class:`ContextItem` for
``context_item``-shaped SELECTs. Our KNN SELECT joins ``context_item``
and the vec0 table but projects computed columns ``item_id`` and
``distance`` — neither matches the required
``{id, scope, kind, source, visibility, version}`` set, so
``scan_item`` passes the rows through as raw tuples. We consume them
with ``row[0], row[1]`` accordingly — same pattern as
:class:`unictx.storage.searcher_impl.SearcherImpl`.

Type reuse
==========

:class:`VectorHit` and :class:`VectorQuery` are defined once in
``unictx.search.vectorstore`` (the Protocol module from Phase 1). This
module re-exports :class:`VectorHit` (and :class:`VectorStoreImpl`) so
tests can import from the impl — matching the existing pattern in
``searcher_impl.py`` which exports :class:`SearchHit`.
"""

from __future__ import annotations

import sqlite3

import sqlite_vec

from unictx.embed.errors import ModelNotFound
from unictx.search.vectorstore import VectorHit
from unictx.storage.searcher_impl import clamp_limit

__all__ = [
    "ModelNotFound",
    "VectorHit",
    "VectorStoreImpl",
]


# ---------------------------------------------------------------------------
# SQL constants — verbatim port of Go's vectorstore.go.
# ---------------------------------------------------------------------------

# Single-row lookup: returns the per-model vec0 table name. Empty result
# set means the model isn't registered → raise ModelNotFound (Go: scan
# returns sql.ErrNoRows → wrapped by fmt.Errorf).
_VEC_TABLE_SQL = "SELECT vec_table FROM embedding_model WHERE slug = ?"


# ---------------------------------------------------------------------------
# VectorStore impl
# ---------------------------------------------------------------------------


class VectorStoreImpl:
    """SQLite-backed :class:`VectorStore`.

    Constructed with a :mod:`sqlite3` connection (typically produced by
    :func:`unictx.storage.db.open_db` and migrated via
    :func:`unictx.storage.migrations_runner.migrate`). Shares the
    connection with the rest of the storage layer.

    The connection is in autocommit mode (``isolation_level=None`` set
    by :func:`open_db`); :meth:`put` issues explicit ``BEGIN``/``COMMIT``
    to keep the DELETE+INSERT atomic.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    # ---- helpers ---------------------------------------------------------

    def _vec_table(self, model_slug: str) -> str:
        """Resolve the vec0 table name for *model_slug*.

        Returns the ``vec_table`` column value from ``embedding_model``.
        Raises :class:`ModelNotFound` if no row matches (Go wraps
        ``sql.ErrNoRows`` with the lookup context — we surface a typed
        error so callers can distinguish model errors from item errors).
        """
        cur = self._db.execute(_VEC_TABLE_SQL, (model_slug,))
        row = cur.fetchone()
        if row is None:
            raise ModelNotFound(model_slug)
        # row is a raw tuple (not context_item-shaped) — see module docstring.
        return row[0]

    # ---- writes ----------------------------------------------------------

    def put(self, model_slug: str, item_id: str, vector: list[float]) -> None:
        """UPSERT embedding via DELETE+INSERT in a single transaction.

        vec0 doesn't support ``INSERT OR REPLACE`` on its TEXT PK (the
        underlying API errors with a UNIQUE constraint failure), so the
        idempotent write is delete-then-insert. Wrapped in an explicit
        ``BEGIN``/``COMMIT`` because the connection is in autocommit mode
        (``isolation_level=None``) — without the explicit tx, the two
        statements would be auto-committed separately and a partial
        failure could leave the table without the row.

        Parameter order is ``(model_slug, item_id, vector)`` — matches the
        :class:`unictx.search.vectorstore.VectorStore` Protocol and Go's
        ``Put(ctx, model, itemID, vector)``.

        Mirrors Go's ``Put``.
        """
        table = self._vec_table(model_slug)
        blob = sqlite_vec.serialize_float32(vector)

        # Explicit BEGIN/COMMIT — db is in autocommit mode. Mirrors Go's
        # tx, err := s.db.BeginTx(ctx, nil); defer tx.Rollback(); tx.Commit().
        self._db.execute("BEGIN")
        try:
            self._db.execute(f"DELETE FROM {table} WHERE item_id = ?", (item_id,))
            self._db.execute(
                f"INSERT INTO {table} (item_id, embedding) VALUES (?, ?)",
                (item_id, blob),
            )
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def delete(self, model_slug: str, item_id: str) -> None:
        """Delete the embedding for *item_id* under *model_slug*.

        Parameter order is ``(model_slug, item_id)`` — matches the
        :class:`unictx.search.vectorstore.VectorStore` Protocol and Go's
        ``Delete(ctx, model, itemID)``.

        No-op if the (item, model) pair was never put — mirrors Go's
        ``Delete`` (which also doesn't check rowcount; the vec0 DELETE
        is naturally idempotent).
        """
        table = self._vec_table(model_slug)
        self._db.execute(f"DELETE FROM {table} WHERE item_id = ?", (item_id,))

    # ---- reads -----------------------------------------------------------

    def search(
        self,
        vector: list[float],
        model_slug: str,
        limit: int,
        *,
        scopes: list[str] | None = None,
        kinds: list[str] | None = None,
        project_id: str = "",
    ) -> list[VectorHit]:
        """KNN query with optional scope/kind/project_id filter pushdown.

        Returns hits ordered by ``distance`` ASC (best match first).
        Filters are pushed down to ``context_item`` via JOIN, so this
        method returns at most ``limit`` hits — no post-filter, no
        re-multiplication. ``limit`` is normalized via :func:`clamp_limit`
        (``<=0 -> 20``, ``>200 -> 200``, unchanged otherwise) before the
        query runs.

        ``k = ?`` (vec0 KNN syntax, NOT ``LIMIT ?``) is the K parameter.
        The MATCH predicate against the embedding column is mandatory —
        without it vec0 falls back to a scan and ``distance`` isn't a
        valid column, surfacing as ``datatype mismatch``.

        project_id (P1 access direction): when non-empty, restricts
        project-scope rows to those whose project_id matches. Global
        rows (scope='global') are never project-scoped and bypass this
        predicate, so a PROJECT actor still sees shared global content.
        This is the row-level isolation complement to the scope-level
        convergence done in SearchService.

        Score: cosine distance ∈ ``[0, 2]`` → similarity ∈ ``[0, 1]``
        via ``score = 1 - distance / 2``. Higher = better.
        """
        table = self._vec_table(model_slug)
        blob = sqlite_vec.serialize_float32(vector)
        k = clamp_limit(limit)

        # Build the optional filter SQL and args. The MATCH clause is
        # mandatory (see module docstring); filters AND onto it. We
        # append " AND " + joined clauses only when at least one filter
        # is supplied (matches Go's `if len(q.Scopes) > 0 || len(q.Kinds) > 0`).
        parts: list[str] = []
        args: list[object] = [blob]
        if scopes:
            parts.append(f"ci.scope IN ({', '.join(['?'] * len(scopes))})")
            args.extend(scopes)
        if kinds:
            parts.append(f"ci.kind IN ({', '.join(['?'] * len(kinds))})")
            args.extend(kinds)
        if project_id:
            # Project isolation: a PROJECT actor sees only its own
            # project rows OR any global row (global is shared). User
            # rows can't appear here because scope convergence already
            # removed 'user' from scopes for a PROJECT actor.
            parts.append("(ci.project_id = ? OR ci.scope = 'global')")
            args.append(project_id)
        filter_sql = (" AND " + " AND ".join(parts)) if parts else ""

        args.append(k)
        query = (
            f"SELECT v.item_id, v.distance "
            f"FROM {table} v "
            f"JOIN context_item ci ON ci.id = v.item_id "
            f"WHERE v.embedding MATCH ?{filter_sql} AND k = ? "
            f"ORDER BY v.distance"
        )

        # row_factory (scan_item) passes through raw tuples here —
        # the SELECT carries computed columns {item_id, distance}, not
        # the full context_item shape. See module docstring.
        cur = self._db.execute(query, args)
        rows = cur.fetchall()

        hits: list[VectorHit] = []
        for row in rows:
            item_id = row[0]
            distance = row[1]
            hits.append(
                VectorHit(
                    id=item_id,
                    distance=float(distance),
                    score=1.0 - float(distance) / 2.0,
                )
            )
        return hits
