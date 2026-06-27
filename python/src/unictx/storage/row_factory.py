"""Row factory that converts SELECT rows into ContextItem instances.

Registered as ``db.row_factory`` by :func:`unictx.storage.db.open_db` so
every SELECT against ``context_item`` returns a :class:`ContextItem`
directly, instead of a raw tuple. The repo impl (Task 2.3
``repo_impl.py``) and the searcher impl (Task 2.4) both rely on this.
Non-``context_item`` SELECTs (e.g. ``SELECT vec_version()``) pass
through as raw tuples — see :func:`scan_item` for the sniff logic.

Column-name lookup
==================

The factory maps columns by **name** (via ``cursor.description``) rather
than by position. Two reasons:

1. **Robustness against column-order drift.** The Go code scans by
   position because Go's ``rows.Scan`` requires it; if the SQL writer
   reorders columns, the Go code silently breaks. Name lookup localizes
   the coupling to the column names themselves, not their order.
2. **Single source of truth.** The column-name list lives in the schema
   (``migrations/0001_init.sql``) and the SELECT statements in
   ``repo_impl.py`` — the row factory reads from ``cursor.description``
   at call time, so any future column rename surfaces immediately as a
   ``KeyError`` rather than a silent mis-scan.

JSON decoding
=============

``tags`` (a JSON array of strings) and ``source_meta`` (a JSON object)
are stored as JSON text. The factory decodes them with the stdlib
:mod:`json` module. NULL or empty strings fall back to the
:class:`ContextItem` defaults (``[]`` and ``{}``).

NULL handling
=============

SQLite NULLs surface as Python ``None``. Several optional columns
(``owner_user_id``, ``project_id``, ...) are NULL when empty. The
factory coerces ``None`` → ``""`` for those string fields to match
:class:`ContextItem`'s in-memory representation, which uses empty
strings for "unset" (mirrors Go's ``sql.NullString.String`` zero value).

Enums
=====

``scope``, ``kind``, ``source``, ``visibility`` are stored as TEXT and
constructed via the enum constructor — invalid values raise
:class:`ValueError` immediately, which is the right failure mode (the
DB should never contain invalid enum values; if it does, we want to
know loudly).
"""

from __future__ import annotations

import json
from sqlite3 import Cursor
from typing import Any

from unictx.items.models import ContextItem, Kind, Scope, Source, Visibility

__all__ = ["scan_item"]


def scan_item(cursor: Cursor, row: tuple[Any, ...]) -> ContextItem | tuple[Any, ...]:
    """Convert one ``context_item`` row into a :class:`ContextItem`.

    Registered as ``db.row_factory``. SQLite calls this once per row with
    the originating cursor (so we can read ``cursor.description``) and
    the row tuple. We rebuild a column-name → value mapping, decode the
    two JSON columns, and construct the dataclass.

    **Non-context_item SELECTs pass through as raw tuples.** The
    connection-level row_factory fires for every SELECT issued against
    the connection — including ``SELECT vec_version()`` from the open_db
    ping, ``SELECT value FROM schema_meta`` from the migration runner,
    and arbitrary diagnostic queries. We sniff the column names: if the
    expected ``context_item`` columns are absent, return the row
    unchanged. This avoids forcing every other SELECT site to opt out
    via per-cursor ``row_factory = None``.
    """
    # cursor.description is a sequence of 7-tuples; [0] is the column
    # name. Using dict-zip here is both robust to column reordering and
    # cheap (small N).
    cols = [desc[0] for desc in cursor.description]

    # Passthrough for non-context_item SELECTs. Checking just the
    # identity-and-enum columns (which a context_item SELECT always
    # carries) is enough — no other table in the schema has all of
    # {id, scope, kind, source, visibility, version} together.
    required = {"id", "scope", "kind", "source", "visibility", "version"}
    if not required.issubset(set(cols)):
        return row

    raw: dict[str, Any] = dict(zip(cols, row, strict=True))

    # JSON columns: NULL or empty string → defaults. The schema declares
    # both as NOT NULL DEFAULT '[]'/{}', but defensive handling covers a
    # manually-edited DB or a future schema where the column becomes
    # nullable.
    tags_raw = raw.get("tags") or "[]"
    meta_raw = raw.get("source_meta") or "{}"

    return ContextItem(
        id=raw["id"],
        scope=Scope(raw["scope"]),
        kind=Kind(raw["kind"]),
        source=Source(raw["source"]),
        # NULL → "" for optional string fields. Mirrors Go's
        # sql.NullString.String zero value.
        owner_user_id=raw.get("owner_user_id") or "",
        project_id=raw.get("project_id") or "",
        agent_id=raw.get("agent_id") or "",
        conversation_id=raw.get("conversation_id") or "",
        parent_id=raw.get("parent_id") or "",
        title=raw.get("title") or "",
        summary=raw.get("summary") or "",
        content=raw.get("content") or "",
        content_uri=raw.get("content_uri") or "",
        content_mime=raw.get("content_mime") or "",
        content_hash=raw.get("content_hash") or "",
        language=raw.get("language") or "",
        tags=json.loads(tags_raw),
        source_meta=json.loads(meta_raw),
        visibility=Visibility(raw["visibility"]),
        confidence=raw["confidence"],
        word_count=raw["word_count"],
        any_embedding=raw["any_embedding"],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        version=raw["version"],
    )
