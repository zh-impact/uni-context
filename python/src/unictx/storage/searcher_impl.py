"""SQLite-backed Searcher with FTS5 + LIKE fallback.

Ports Go's ``archive/go/internal/adapter/sqlite/searcher.go``. This is
the concrete storage-side impl of the (forthcoming) Searcher Protocol —
:class:`unictx.search.service.SearchService` (Phase 5) consumes it.

Two query paths
===============

* **FTS5 MATCH** — for queries with >= 3 Unicode code points. BM25
  ranking, title-column snippet. CJK is supported via the trigram
  tokenizer (``context_fts`` is created with ``tokenize='trigram'`` in
  migration 0001).
* **LIKE fallback** — for queries with < 3 code points. The FTS5
  trigram tokenizer requires phrases of >= 3 code points, so 2-char
  CJK queries like ``部署`` silently return zero results. LIKE matches
  substrings directly across title / summary / content with no minimum.

Both paths apply identical ``clamp_limit`` semantics.

Malformed-FTS bugfix
====================

``search_fts`` extracts a snippet from the TITLE column only (FTS5
column index 0). Content-column snippets were removed because
``context_fts`` is configured as an external-content table
(``content='context_item'``) and IngestService externalizes large
content to FileStore after the FTS row was written via
:meth:`ContextRepoImpl.reindex_fts` (which bypasses the AFTER UPDATE
trigger pair). The resulting divergence — FTS inverted index has the
tokens, ``context_item.content`` is empty — makes FTS5's
``snippet(context_fts, 2, ...)`` detect the inconsistency and return
``SQLITE_CORRUPT_VTAB``, surfaced as "database disk image is
malformed". This aborted the entire search for any externalized item.

The title column is always inline in ``context_item``, so the title
snippet remains safe. See the CRITICAL regression test
``test_externalized_content_does_not_corrupt`` for the load-bearing
assertion.

Row consumption
===============

The connection's ``row_factory`` is :func:`unictx.storage.row_factory.scan_item`,
which inspects column names and only returns :class:`ContextItem` for
``context_item``-shaped SELECTs. Our Searcher SQL joins
``context_fts`` and ``context_item`` and returns computed columns
(``id``, ``score``, ``snippet``); the column-name set does not
match the context_item shape, so ``scan_item`` passes the rows through
as raw tuples. We consume them with ``fetchall()[0], row[1], ...``
accordingly. The ``_get_hit_content`` test helper confirms the same
passthrough behavior for ``SELECT content FROM context_item``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

__all__ = [
    "SearchHit",
    "SearcherImpl",
    "clamp_limit",
    "fts_query_string",
    "like_pattern",
]


# ---------------------------------------------------------------------------
# Constants — limit semantics (commit 4d26cea regression).
# ---------------------------------------------------------------------------

# Default applied when the caller passes <= 0 (treats "unset" as the
# default). Mirrors Go's defaultLimit.
_DEFAULT_LIMIT = 20

# Upper bound on returned rows. The service-layer over-fetch
# (search.go overFetch = limit * 3) can pass Limit=300 for a
# user-requested limit=100 — clamping (rather than resetting to
# _DEFAULT_LIMIT) preserves the caller's explicit over-fetch headroom
# without unbounding the query. Mirrors Go's maxLimit.
_MAX_LIMIT = 200


def clamp_limit(n: int) -> int:
    """Normalize a caller-supplied limit.

    * ``n <= 0`` -> :data:`_DEFAULT_LIMIT` (treats "unset" as default).
    * ``n > _MAX_LIMIT`` -> :data:`_MAX_LIMIT` (preserves over-fetch
      headroom without unbounding the query; previously this branch
      reset to :data:`_DEFAULT_LIMIT`, which silently destroyed the
      caller's explicit over-fetch — commit 4d26cea bugfix).
    * otherwise -> ``n`` unchanged.

    Used by both the FTS and LIKE paths so they apply identical
    semantics. **Asymmetric with** :meth:`ContextRepoImpl.list`'s clamp
    (``<=0 or >200 -> 50``) — preserved from Go for parity.
    """
    if n <= 0:
        return _DEFAULT_LIMIT
    if n > _MAX_LIMIT:
        return _MAX_LIMIT
    return n


# ---------------------------------------------------------------------------
# SQL — copied verbatim from Go's searcher.go to keep the two ports
# reviewable side-by-side.
# ---------------------------------------------------------------------------

# Title-snippet ONLY. NO content-column snippet (would trigger
# SQLITE_CORRUPT_VTAB on externalized-content rows — see module
# docstring). snippet(context_fts, 0, ...) extracts from column index
# 0 (title). The ellipsis argument is '…' to match Go's verbatim.
_SEARCH_SQL = """
SELECT ci.id, bm25(context_fts) AS score,
       snippet(context_fts, 0, '', '', '…', 16) AS snippet
FROM context_fts
JOIN context_item ci ON ci.rowid = context_fts.rowid
WHERE context_fts MATCH ?
ORDER BY bm25(context_fts)
LIMIT ?
"""

# LIKE fallback for queries < 3 code points. Score is a constant 1.0
# (no relevance ranking — every match is equal) and results are
# ordered by created_at DESC for deterministic output. Unindexed scan
# on context_item — acceptable for the expected <10k personal-note
# scale. Matches title / summary / content (3 columns) — same as Go's
# likeSearchSQL.
_LIKE_SEARCH_SQL = """
SELECT ci.id, 1.0 AS score
FROM context_item ci
WHERE ci.title LIKE ? ESCAPE '\\'
   OR ci.summary LIKE ? ESCAPE '\\'
   OR ci.content LIKE ? ESCAPE '\\'
ORDER BY ci.created_at DESC
LIMIT ?
"""


# ---------------------------------------------------------------------------
# Helpers — direct ports of Go's ftsQueryString / likePattern.
# ---------------------------------------------------------------------------


def fts_query_string(raw: str) -> str:
    """Build a safe FTS5 phrase query by wrapping *raw* in double quotes.

    Embedded ``"`` characters are doubled (FTS5's escape for embedded
    quotes within a phrase). This prevents FTS5 operator injection
    (AND / OR / NEAR / ``^`` / column filters) from user input.

    ``a"b`` -> ``"a""b"``

    Empty / whitespace-only input returns ``""`` (caller treats empty
    as "no search" and short-circuits).

    Note: we deliberately do NOT ``strip()`` the body, because leading
    or trailing whitespace may be a load-bearing part of a trigram
    phrase (e.g. ``"部署 "`` as a 4-rune phrase including the trailing
    ASCII space). Only all-whitespace input is rejected as empty.
    Mirrors Go's ftsQueryString exactly.
    """
    if raw.strip() == "":
        return ""
    escaped = raw.replace('"', '""')
    return '"' + escaped + '"'


def like_pattern(raw: str) -> str:
    """Escape LIKE wildcards (``%``, ``_``, ``\\``) in *raw* and wrap
    in ``%...%`` for substring match.

    The ESCAPE ``'\\'`` clause in :data:`_LIKE_SEARCH_SQL` activates
    backslash as the escape character.

    Order matters: ``\\`` must be escaped FIRST, otherwise the escape
    character itself gets escaped twice (e.g. raw ``a\\b`` -> wrong
    ``%a\\\\b%`` instead of correct ``%a\\\\b%``).

    Mirrors Go's likePattern.
    """
    r = raw.replace("\\", "\\\\")
    r = r.replace("%", "\\%")
    r = r.replace("_", "\\_")
    return "%" + r + "%"


def _rune_count(s: str) -> int:
    """Count Unicode code points in *s*.

    Python 3 strings are already sequences of Unicode code points
    (PEP 393, Python 3.3+), so this is just :func:`len`. We keep the
    helper to mirror Go's ``utf8.RuneCountInString`` semantically —
    the name documents the parity rationale at the call site. If this
    code ever needs to run on a non-CPython build or future Python
    where strings might use a different internal representation, the
    one-liner here is the single point of change.
    """
    return len(s)


# ---------------------------------------------------------------------------
# Result dataclass.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SearchHit:
    """One search result.

    Mirrors Go's ``port.SearchHit``. ``score`` is negated bm25 so
    higher = better match (SQLite's ``bm25()`` returns negative
    scores; lower = better. We flip the sign for caller ergonomics).
    ``snippet`` is a fragment of the title column; empty when the
    match was via content tokens (no content snippet is returned) or
    when the query fell through the LIKE fallback path.
    """

    id: str
    score: float
    snippet: str


# ---------------------------------------------------------------------------
# Searcher
# ---------------------------------------------------------------------------


class SearcherImpl:
    """SQLite-backed Searcher (FTS5 + LIKE fallback).

    Constructed with a :mod:`sqlite3` connection (typically produced
    by :func:`unictx.storage.db.open_db` and migrated via
    :func:`unictx.storage.migrations_runner.migrate`). Shares the
    connection with the rest of the storage layer.

    The single public entry point is :meth:`search`; it dispatches to
    the FTS or LIKE path based on the query's code-point count.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def search(self, query: str, limit: int = 20) -> list[SearchHit]:
        """Search for *query*, returning at most *limit* hits.

        Dispatches to the FTS path for queries with >= 3 code points
        (trigram tokenizer minimum) and to the LIKE fallback
        otherwise. Empty / whitespace-only queries return ``[]``
        without hitting either path.

        *limit* is normalized via :func:`clamp_limit` (``<=0 -> 20``,
        ``>200 -> 200``, unchanged otherwise) on both paths.
        """
        if query.strip() == "":
            return []

        if _rune_count(query) < 3:
            return self._like_search(query.strip(), limit)
        return self._search_fts(query, limit)

    # ---- FTS path --------------------------------------------------------

    def _search_fts(self, query: str, limit: int) -> list[SearchHit]:
        ftsq = fts_query_string(query)
        if ftsq == "":
            return []
        clamped = clamp_limit(limit)

        # row_factory (scan_item) passes through raw tuples here —
        # the SELECT carries computed columns {id, score, snippet},
        # not the full context_item shape. See module docstring.
        cur = self._db.execute(_SEARCH_SQL, (ftsq, clamped))
        rows = cur.fetchall()

        hits: list[SearchHit] = []
        for row in rows:
            row_id = row[0]
            score = row[1]
            snippet = row[2]
            # bm25 returns negative scores (more negative = better
            # match). Negate so higher = better — mirrors Go.
            hits.append(
                SearchHit(
                    id=row_id,
                    score=-score,
                    snippet=snippet if snippet is not None else "",
                )
            )
        return hits

    # ---- LIKE path -------------------------------------------------------

    def _like_search(self, query: str, limit: int) -> list[SearchHit]:
        clamped = clamp_limit(limit)
        pattern = like_pattern(query)

        # Three placeholders — one per ORed column (title, summary,
        # content). Same as Go's likeSearchSQL.
        cur = self._db.execute(_LIKE_SEARCH_SQL, (pattern, pattern, pattern, clamped))
        rows = cur.fetchall()

        hits: list[SearchHit] = []
        for row in rows:
            row_id = row[0]
            score = row[1]
            hits.append(
                SearchHit(
                    id=row_id,
                    score=float(score),
                    snippet="",  # LIKE path leaves snippet empty by design.
                )
            )
        return hits
