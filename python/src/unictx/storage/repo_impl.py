"""SQLite-backed :class:`ContextRepo` implementation.

Ports Go's ``archive/go/internal/adapter/sqlite/repo.go``. This is the
concrete storage-side impl of the :class:`unictx.items.repo.ContextRepo`
Protocol — it talks directly to a :mod:`sqlite3` connection produced by
:func:`unictx.storage.db.open_db`.

The connection's ``row_factory`` is :func:`unictx.storage.row_factory.scan_item`
(set by :func:`open_db`), so SELECTs against ``context_item`` return
:class:`ContextItem` instances directly — no manual tuple unpacking.

Method-by-method mapping to Go
==============================

* ``create`` — Go ``Create``. INSERT with JSON-encoded tags and
  source_meta. Empty optional strings go to SQL NULL via :func:`_nullable`
  (Go's ``nullable`` helper) to match Go's storage footprint.
* ``get`` — Go ``Get``. SELECT; ``fetchone() is None`` mirrors Go's
  ``errors.Is(err, sql.ErrNoRows)`` → raise :class:`ItemNotFound`.
* ``update`` — Go ``Update``. **Mutates the caller's item** by
  incrementing ``version`` and refreshing ``updated_at`` BEFORE the
  UPDATE runs (port of Go's ``item.Version++`` / ``item.UpdatedAt = ...``
  side effect). Returns the same mutated instance. See the method
  docstring for why we preserve this Go cosmetic.
* ``delete`` — Go ``Delete``. ``rowcount == 0`` → raise
  :class:`ItemNotFound` (Go's ``RowsAffected==0`` check).
* ``list`` — Go ``List``. Dynamic WHERE clause from :class:`ItemFilter`,
  ORDER BY ``created_at DESC, id DESC``, LIMIT ``filter.limit + 1`` to
  detect a next page. Cursor decode: see :func:`decode_cursor`.
* ``next_cursor`` — Go ``NextCursor``. ``encode_cursor(item.created_at, item.id)``.
* ``reindex_fts`` — Go ``ReindexFTS``. Two-statement delete-then-insert
  pattern required for FTS5 external-content tables (the only way to
  rewrite a row). Wrapped in an explicit BEGIN/COMMIT because
  ``cursor.execute`` runs ONE statement (Go's ``ExecContext`` could run
  both in one call). ``rowcount == 0`` on the delete → raise
  :class:`ItemNotFound`.

Cursor encoding
===============

The pagination cursor is the string ``f"{base36(ts)}:{item_id}"`` where
``ts`` is the item's ``created_at`` unix timestamp. This is byte-identical
to Go's ``strconv.FormatInt(ts, 36) + ":" + id`` — verified by the
migration spike (``python/spikes/migration-spike/spike.py``) and by the
``test_cursor_byte_identical_with_go`` test here. The
``encode_cursor``/``decode_cursor`` pair is copied verbatim from the
spike.

Limit clamping
==============

``list`` clamps ``limit <= 0 or limit > 200 → 50`` to match Go's
``List``. **This is asymmetric with** :class:`unictx.storage.searcher_impl.Searcher`'s
clamp, which uses ``<=0 → 20`` and ``>200 → 200``. The asymmetry is
intentional and inherited from Go; we preserve it for parity.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from unictx.items.errors import ItemNotFound
from unictx.items.models import ContextItem
from unictx.items.repo import ItemFilter

__all__ = ["ContextRepoImpl", "encode_cursor", "decode_cursor"]


# ---------------------------------------------------------------------------
# SQL constants — copied verbatim from Go's repo.go to keep the two ports
# reviewable side-by-side. Column order in getItemSQL and the list query MUST
# match (the row_factory reads by name, but parity avoids drift).
# ---------------------------------------------------------------------------

_INSERT_ITEM_SQL = """
INSERT INTO context_item (
    id, scope, kind, source, owner_user_id, project_id, agent_id,
    conversation_id, parent_id, title, summary, content, content_uri,
    content_mime, content_hash, language, tags, source_meta, visibility,
    confidence, word_count, any_embedding, created_at, updated_at, version
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_GET_ITEM_SQL = """
SELECT id, scope, kind, source, owner_user_id, project_id, agent_id,
       conversation_id, parent_id, title, summary, content, content_uri,
       content_mime, content_hash, language, tags, source_meta, visibility,
       confidence, word_count, any_embedding, created_at, updated_at, version
FROM context_item WHERE id = ?
"""

_UPDATE_ITEM_SQL = """
UPDATE context_item SET
    title=?, summary=?, content=?, content_uri=?, content_mime=?,
    content_hash=?, language=?, tags=?, source_meta=?, visibility=?,
    confidence=?, word_count=?, any_embedding=?, updated_at=?, version=?
WHERE id=?
"""

# Columns selected by list() — must match getItemSQL's column set so the
# row_factory returns the same dataclass shape from both code paths.
_LIST_SELECT_COLUMNS = """
       id, scope, kind, source, owner_user_id, project_id, agent_id,
       conversation_id, parent_id, title, summary, content, content_uri,
       content_mime, content_hash, language, tags, source_meta, visibility,
       confidence, word_count, any_embedding, created_at, updated_at, version
"""

# FTS5 external-content rewrite: two statements. Go concatenates them and
# ships to ExecContext in one call; Python's cursor.execute runs only one,
# so we issue them as two execute() calls inside an explicit transaction.
# See reindex_fts() for the orchestration.
_FTS_DELETE_SQL = (
    "INSERT INTO context_fts(context_fts, rowid, title, summary, content) "
    "SELECT 'delete', rowid, title, summary, content FROM context_item WHERE id = ?"
)
_FTS_INSERT_SQL = (
    "INSERT INTO context_fts(rowid, title, summary, content) "
    "SELECT rowid, ?, ?, ? FROM context_item WHERE id = ?"
)


# ---------------------------------------------------------------------------
# Cursor encode/decode — verbatim from
# python/spikes/migration-spike/spike.py. Produces byte-identical output to
# Go's strconv.FormatInt(ts, 36) + ":" + id.
# ---------------------------------------------------------------------------

_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def encode_cursor(ts: int, item_id: str) -> str:
    """Encode ``(ts, item_id)`` as ``f"{base36(ts)}:{item_id}"``.

    Mirrors Go's ``strconv.FormatInt(ts, 36)``. The output is the canonical
    short form (no leading zeros, lowercase digits). ``ts == 0`` produces
    ``"0"`` to match ``strconv.FormatInt(0, 36)``.
    """
    if ts == 0:
        return "0:" + item_id
    sign = ""
    n = ts
    if n < 0:
        sign, n = "-", -n
    out = ""
    while n > 0:
        n, rem = divmod(n, 36)
        out = _BASE36_DIGITS[rem] + out
    return sign + out + ":" + item_id


def decode_cursor(cursor: str) -> tuple[int, str]:
    """Inverse of :func:`encode_cursor`. Raises ``ValueError`` if malformed.

    Mirrors Go's ``decodeCursor``. Accepts negative-ts cursors
    (``"-" + base36(...)``) for forward-compat, though no current caller
    produces them.
    """
    head, _, tail = cursor.partition(":")
    if not tail:
        raise ValueError(f"malformed cursor: {cursor!r}")
    sign = 1
    digits = head
    if head.startswith("-"):
        sign, digits = -1, head[1:]
    ts = int(digits, 36) * sign
    return ts, tail


# ---------------------------------------------------------------------------
# Helpers — direct ports of Go's nullable() and placeholders().
# ---------------------------------------------------------------------------


def _nullable(s: str) -> str | None:
    """Empty string → None (SQL NULL). Mirrors Go's ``nullable`` helper.

    The schema declares these columns as nullable TEXT; storing NULL
    instead of "" matches Go's footprint and lets ``IS NULL`` predicates
    work the same way in both ports.
    """
    if s == "":
        return None
    return s


def _placeholders(n: int) -> str:
    """Build ``"?, ?, ..., ?"`` with *n* placeholders.

    Mirrors Go's ``placeholders`` helper (``strings.Repeat("?,", n-1) + "?"``).
    """
    return ", ".join(["?"] * n)


def _now_unix() -> int:
    """Current UTC time as integer unix timestamp (matches Go's ``time.Now().Unix()``)."""
    return int(datetime.now(UTC).timestamp())


def _json_dumps(value: Any) -> str:
    """Compact JSON encoding to match Go's ``encoding/json`` output.

    Go's Marshal produces no extraneous whitespace (``","`` and ``":"``
    separators); Python's default :func:`json.dumps` adds a space after
    each. The explicit separators here keep the stored bytes byte-identical
    across the two implementations, which matters for hashes and tests
    that compare DB contents directly.
    """
    return json.dumps(value, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class ContextRepoImpl:
    """SQLite-backed :class:`ContextRepo`.

    Constructed with a :mod:`sqlite3` connection (typically produced by
    :func:`unictx.storage.db.open_db` and migrated via
    :func:`unictx.storage.migrations_runner.migrate`). The same connection
    is shared with other storage impls (searcher, vectorstore, embedding
    repo) — they all live in one process, talk to one DB file, and rely
    on SQLite's own locking.

    The connection's ``row_factory`` is :func:`scan_item`, so SELECTs
    return :class:`ContextItem` instances directly. The repo never builds
    a ContextItem by hand from a row tuple.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    # ---- writes ----------------------------------------------------------

    def create(self, item: ContextItem) -> None:
        """INSERT a new item. Tags and source_meta are JSON-encoded.

        Mirrors Go's ``Create``. Empty optional strings become SQL NULL
        via :func:`_nullable` to match Go's storage footprint (so the
        same DB written by either port reads back identically).
        """
        tags = _json_dumps(item.tags)
        meta = _json_dumps(item.source_meta)
        self._db.execute(
            _INSERT_ITEM_SQL,
            (
                item.id,
                str(item.scope),
                str(item.kind),
                str(item.source),
                _nullable(item.owner_user_id),
                _nullable(item.project_id),
                _nullable(item.agent_id),
                _nullable(item.conversation_id),
                _nullable(item.parent_id),
                item.title,
                item.summary,
                item.content,
                _nullable(item.content_uri),
                _nullable(item.content_mime),
                _nullable(item.content_hash),
                _nullable(item.language),
                tags,
                meta,
                str(item.visibility),
                item.confidence,
                item.word_count,
                item.any_embedding,
                item.created_at,
                item.updated_at,
                item.version,
            ),
        )

    def update(self, item: ContextItem) -> ContextItem:
        """UPDATE an item, returning the same (mutated) instance.

        **Mutates the caller's item** before executing the UPDATE:

        * ``item.version += 1`` (Go: ``item.Version++``)
        * ``item.updated_at = _now_unix()`` (Go: ``item.UpdatedAt = ...``)

        This is a port of Go's known cosmetic issue — the Go method
        documents this as a "minor" item in the task plan and we
        preserve the behavior for parity. Callers that want to keep the
        pre-update state must copy the item first.

        Raises :class:`ItemNotFound` if no row matches ``item.id`` (Go's
        ``RowsAffected == 0`` check).
        """
        # Mutate BEFORE the UPDATE runs — Go's order. If the UPDATE
        # fails (item missing), the caller's item has still been bumped;
        # this matches Go's behavior and is the documented cosmetic.
        item.version += 1
        item.updated_at = _now_unix()

        tags = _json_dumps(item.tags)
        meta = _json_dumps(item.source_meta)
        cur = self._db.execute(
            _UPDATE_ITEM_SQL,
            (
                item.title,
                item.summary,
                item.content,
                _nullable(item.content_uri),
                _nullable(item.content_mime),
                _nullable(item.content_hash),
                _nullable(item.language),
                tags,
                meta,
                str(item.visibility),
                item.confidence,
                item.word_count,
                item.any_embedding,
                item.updated_at,
                item.version,
                item.id,
            ),
        )
        if cur.rowcount == 0:
            raise ItemNotFound(item.id)
        return item

    def delete(self, id: str) -> None:
        """DELETE an item. Raises :class:`ItemNotFound` if no row matches."""
        cur = self._db.execute("DELETE FROM context_item WHERE id=?", (id,))
        if cur.rowcount == 0:
            raise ItemNotFound(id)

    # ---- reads -----------------------------------------------------------

    def get(self, id: str) -> ContextItem:
        """SELECT an item by id. Raises :class:`ItemNotFound` if missing.

        ``fetchone() is None`` mirrors Go's
        ``errors.Is(err, sql.ErrNoRows)``. The row_factory (scan_item)
        converts the row to a :class:`ContextItem` for us.
        """
        cur = self._db.execute(_GET_ITEM_SQL, (id,))
        row = cur.fetchone()
        if row is None:
            raise ItemNotFound(id)
        # scan_item returns ContextItem for context_item SELECTs (and
        # passes through raw tuples otherwise). _GET_ITEM_SQL selects the
        # full context_item column set, so row is always a ContextItem
        # here — narrow the type for the caller.
        return row  # type: ignore[return-value]

    def list(self, filter: ItemFilter) -> tuple[list[ContextItem], str]:
        """Paginated list with cursor.

        Returns ``(rows, next_cursor)``. ``next_cursor`` is ``""`` if no
        more rows remain (the LIMIT+1 sentinel was not consumed).

        ORDER BY ``created_at DESC, id DESC`` — the cursor encodes
        ``(created_at, id)`` and the WHERE clause resumes with
        ``(created_at < ? OR (created_at = ? AND id < ?))``.

        Limit clamping: ``<=0 or >200 → 50``. **Asymmetric with the
        Searcher** (which uses ``<=0 → 20`` and ``>200 → 200``) —
        preserved from Go for parity.
        """
        # Local mutation of the filter so we don't surprise the caller.
        limit = filter.limit
        if limit <= 0 or limit > 200:
            limit = 50

        where: list[str] = []
        args: list[Any] = []

        if filter.scopes:
            where.append(f"scope IN ({_placeholders(len(filter.scopes))})")
            args.extend(str(s) for s in filter.scopes)
        if filter.kinds:
            where.append(f"kind IN ({_placeholders(len(filter.kinds))})")
            args.extend(str(k) for k in filter.kinds)
        if filter.owner_user_id:
            where.append("owner_user_id=?")
            args.append(filter.owner_user_id)
        if filter.project_id:
            where.append("project_id=?")
            args.append(filter.project_id)
        if filter.tags:
            # OR semantics: an item matches if it carries ANY of the
            # requested tags. Tags are stored as a JSON array; json_each
            # expands the array and we test membership against the
            # supplied filter set.
            where.append(
                f"EXISTS (SELECT 1 FROM json_each(tags) je "
                f"WHERE je.value IN ({_placeholders(len(filter.tags))}))"
            )
            args.extend(filter.tags)
        if filter.any_embedding is not None:
            # Tri-state: None = no filter, 0 = unembedded only,
            # 1 = embedded only. Backfill passes 0 so already-embedded
            # items never enter the iteration.
            where.append("any_embedding = ?")
            args.append(filter.any_embedding)
        if filter.not_done_for_model:
            # Plan 2c: ReembedService wants items lacking a status='done'
            # row for the active model. NOT EXISTS preserves the query
            # plan; rewriting as JOIN would risk changing it.
            where.append(
                "NOT EXISTS ("
                "SELECT 1 FROM context_embedding ce "
                "WHERE ce.item_id = context_item.id "
                "AND ce.model_slug = ? "
                "AND ce.status = 'done'"
                ")"
            )
            args.append(filter.not_done_for_model)
        if filter.cursor:
            ts, id_from_cursor = decode_cursor(filter.cursor)
            where.append("(created_at < ? OR (created_at = ? AND id < ?))")
            args.extend([ts, ts, id_from_cursor])

        # Trailing "1=1" lets every clause above be AND-joined without a
        # special case for the empty-WHERE situation. Matches Go's shape.
        where.append("1=1")

        query = (
            f"SELECT {_LIST_SELECT_COLUMNS} "
            "FROM context_item "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC "
            "LIMIT ?"
        )
        args.append(limit + 1)  # +1 to detect next page

        cur = self._db.execute(query, args)
        rows = cur.fetchall()

        next_cursor = ""
        if len(rows) > limit:
            rows = rows[:limit]
            # Build cursor from the last VISIBLE row (after slicing).
            next_cursor = self.next_cursor(rows[-1])

        return rows, next_cursor

    def next_cursor(self, item: ContextItem) -> str:
        """Cursor for the item that ends a page (caller passes last visible row)."""
        return encode_cursor(item.created_at, item.id)

    # ---- FTS reindex -----------------------------------------------------

    def reindex_fts(self, id: str, title: str, summary: str, content: str) -> None:
        """Rewrite the FTS row for an item. Idempotent.

        Used by IngestService when content was externalized — the AFTER
        INSERT trigger captured empty content, making the item
        unsearchable. For inline items this is a harmless overwrite.

        Implementation note: FTS5 external-content tables cannot be
        UPDATEd directly — the only way to change a row is the
        delete-then-insert special-command pattern. The 'delete' row
        needs the SAME column values that were originally inserted, so
        we SELECT them from ``context_item``. The replacement INSERT
        uses caller-supplied title/summary/content (typically the
        hydrated bytes from FileStore).

        Two-statement atomicity: Go runs both in one ``ExecContext``;
        Python's ``cursor.execute`` runs only one statement, so we wrap
        them in an explicit ``BEGIN``/``COMMIT`` to preserve atomicity.
        If the delete succeeds but the insert fails, FTS5 leaves the
        row missing — caller can retry ``reindex_fts`` (idempotent).

        Raises :class:`ItemNotFound` if no row matches ``id`` (the
        delete statement affected zero rows).
        """
        self._db.execute("BEGIN")
        try:
            cur = self._db.execute(_FTS_DELETE_SQL, (id,))
            deleted = cur.rowcount
            self._db.execute(_FTS_INSERT_SQL, (title, summary, content, id))
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

        # Go sums rowcount across both statements; here we have the
        # delete's rowcount alone. Zero means the item doesn't exist —
        # no row to delete, no row to insert.
        if deleted == 0:
            raise ItemNotFound(id)
