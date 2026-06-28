"""Tests for unictx.storage.searcher_impl.SearcherImpl.

Ports the round-trip and edge-case scenarios from Go's
``archive/go/internal/adapter/sqlite/searcher_test.go``. Together these
cover:

* FTS5 MATCH path (basic match, BM25 ranking, no-match, empty query)
* CJK trigram matching (3-char CJK query is the minimum)
* FTS5 phrase-query injection armor (``fts_query_string``)
* LIKE fallback for queries < 3 code points (short CJK and ASCII)
* LIKE wildcard escaping (literal ``%`` / ``_`` in user input)
* ``clamp_limit`` regression (commit 4d26cea: >200 clamps, doesn't reset)
* **CRITICAL**: externalized content does not corrupt FTS — port of
  Go's ``TestSearcher_FTS_ExternalizedContentDoesNotCorrupt``

The fixture ``migrated_db`` (tests/conftest.py) yields a fresh
``:memory:`` connection with all migrations applied. ``_make_item`` is
the equivalent of Go's ``makeItem`` (in-memory item with a fixed
owner). ``_open_with_data`` migrates and inserts the items via
:class:`ContextRepoImpl`, which fires the AFTER INSERT FTS trigger.
"""

from __future__ import annotations

import sqlite3

import pytest

from unictx.items.models import (
    ContextItem,
    Kind,
    NewItemParams,
    Scope,
    Source,
    new_context_item,
)
from unictx.storage.repo_impl import ContextRepoImpl
from unictx.storage.searcher_impl import (
    SearcherImpl,
    SearchHit,
    _rune_count,
    clamp_limit,
    fts_query_string,
    like_pattern,
)

# ---------------------------------------------------------------------------
# Helpers — equivalent of Go's makeItem / openMemWithSampleData.
# ---------------------------------------------------------------------------


def _make_item(title: str, content: str = "", *, id: str | None = None) -> ContextItem:
    """Build a user-scope note. Mirrors Go test helper ``makeItem``.

    Same identity fields as Go's ``makeItem`` (ScopeUser, KindNote,
    SourceManual, owner="u"). Optional ``id`` overrides the generated
    uuid7 so tests can refer to items deterministically.
    """
    item = new_context_item(
        Scope.USER,
        Kind.NOTE,
        Source.MANUAL,
        NewItemParams(owner_user_id="u"),
        title=title,
        content=content,
    )
    if id is not None:
        item.id = id
    return item


@pytest.fixture
def make_db(migrated_db: sqlite3.Connection) -> sqlite3.Connection:
    """Re-export ``migrated_db`` under the Go-test-style name."""
    return migrated_db


def _open_with_data(db: sqlite3.Connection, items: list[ContextItem]) -> ContextRepoImpl:
    """Migrate (already done by fixture) and INSERT via ContextRepoImpl.

    The repo's ``create`` fires the AFTER INSERT trigger that writes the
    FTS row — same as Go's ``NewContextRepo(db).Create(...)``.
    """
    repo = ContextRepoImpl(db)
    for it in items:
        repo.create(it)
    return repo


# ---------------------------------------------------------------------------
# Unit tests — pure-function helpers (no DB).
# ---------------------------------------------------------------------------


class TestClampLimit:
    """clamp_limit regression from commit 4d26cea.

    <= 0 -> default (20). >200 -> max (200), NOT reset to default.
    """

    def test_zero_returns_default(self) -> None:
        assert clamp_limit(0) == 20

    def test_negative_returns_default(self) -> None:
        assert clamp_limit(-1) == 20
        assert clamp_limit(-100) == 20

    def test_one_to_two_hundred_unchanged(self) -> None:
        for n in (1, 50, 100, 199, 200):
            assert clamp_limit(n) == n, f"n={n} should be unchanged"

    def test_above_two_hundred_clamped_not_reset(self) -> None:
        # Regression: the buggy branch reset to default; the fix clamps
        # to max so the service-layer over-fetch headroom is preserved.
        assert clamp_limit(201) == 200
        assert clamp_limit(300) == 200  # search.go overFetch = limit * 3
        assert clamp_limit(10_000) == 200


class TestFtsQueryString:
    """fts_query_string: FTS5 phrase-query injection armor."""

    def test_empty_string_returns_empty(self) -> None:
        assert fts_query_string("") == ""

    def test_whitespace_only_returns_empty(self) -> None:
        assert fts_query_string("   ") == ""
        assert fts_query_string("\t\n") == ""

    def test_plain_word_wrapped_in_quotes(self) -> None:
        assert fts_query_string("hello") == '"hello"'

    def test_embedded_quote_doubled(self) -> None:
        # a"b -> "a""b" — FTS5 phrase-query escape for embedded quotes.
        assert fts_query_string('a"b') == '"a""b"'

    def test_injection_attempt_neutralized(self) -> None:
        # foo" OR 1=1 -> "foo"" OR 1=1" — becomes a literal phrase search.
        assert fts_query_string('foo" OR 1=1') == '"foo"" OR 1=1"'

    def test_leading_trailing_whitespace_preserved_in_body(self) -> None:
        # Go's note: ftsQueryString deliberately does NOT TrimSpace the
        # body, because leading/trailing whitespace may be load-bearing
        # for a trigram phrase ("部署 " is a 4-rune phrase). Only
        # all-whitespace input is rejected as empty.
        assert fts_query_string("部署 ") == '"部署 "'


class TestLikePattern:
    """like_pattern: LIKE wildcard escaping."""

    def test_plain_word_wrapped_in_percent(self) -> None:
        assert like_pattern("hello") == "%hello%"

    def test_percent_escaped(self) -> None:
        assert like_pattern("100%") == r"%100\%%"

    def test_underscore_escaped(self) -> None:
        assert like_pattern("foo_bar") == r"%foo\_bar%"

    def test_backslash_escaped_first(self) -> None:
        # Order matters: backslash must be escaped BEFORE % and _,
        # otherwise the escape character itself gets escaped twice.
        assert like_pattern("a\\b") == r"%a\\b%"

    def test_combined(self) -> None:
        assert like_pattern("a\\b%c_d") == r"%a\\b\%c\_d%"


class TestRuneCount:
    """_rune_count: Unicode code-point counter (Python 3 strings ARE
    already code points, so this is just len() — see helper docstring).
    """

    def test_ascii(self) -> None:
        assert _rune_count("hello") == 5

    def test_cjk(self) -> None:
        # 部署 is 2 code points in 6 UTF-8 bytes.
        assert _rune_count("部署") == 2

    def test_mixed_with_space(self) -> None:
        # "部署 " is 3 code points (incl trailing ASCII space).
        assert _rune_count("部署 ") == 3

    def test_emoji(self) -> None:
        # Surrogate-pair emoji is one code point in Python 3.
        assert _rune_count("😀") == 1


# ---------------------------------------------------------------------------
# Integration tests — FTS path.
# ---------------------------------------------------------------------------


class TestSearchFts:
    """FTS5 MATCH path: basic match, CJK trigram, BM25, no-match, empty."""

    def test_basic_match(self, make_db: sqlite3.Connection) -> None:
        _open_with_data(
            make_db,
            [
                _make_item("如何部署 Go 服务到 k8s", "k8s deployment yaml 示例"),
                _make_item("向量数据库选型对比", "Qdrant vs sqlite-vec"),
                _make_item("Python 部署 Flask 应用", "gunicorn + nginx"),
            ],
        )
        s = SearcherImpl(make_db)
        # "部署 " (部署 + ASCII space) appears in two titles.
        hits = s.search("部署 ", limit=10)
        assert len(hits) == 2
        # Both matched snippets must be one of the two expected titles
        # (BM25 may rank them in either order depending on term freq).
        expected = {
            "如何部署 Go 服务到 k8s",
            "Python 部署 Flask 应用",
        }
        actual = {h.snippet for h in hits}
        assert actual == expected

    def test_cjk_trigram(self, make_db: sqlite3.Connection) -> None:
        # 3-char CJK query — the trigram minimum.
        _open_with_data(
            make_db,
            [
                _make_item("部署文档", "如何部署"),
                _make_item("上线流程", "与部署无关"),
            ],
        )
        s = SearcherImpl(make_db)
        hits = s.search("部署文", limit=5)
        assert len(hits) >= 1

    def test_bm25_ranking(self, make_db: sqlite3.Connection) -> None:
        # Item A has the 3-rune query "部署 A" repeated many times;
        # item B has it once. BM25 should rank A first.
        _open_with_data(
            make_db,
            [
                _make_item("部署 A 部署 A 部署 A", "部署 A部署 A部署 A"),
                _make_item("部署 A", "无关内容"),
            ],
        )
        s = SearcherImpl(make_db)
        hits = s.search("部署 A", limit=5)
        assert len(hits) >= 2
        # Higher-frequency match should rank first. bm25() returns
        # negative scores (more negative = better); the impl negates so
        # higher = better.
        assert hits[0].score >= hits[1].score, (
            f"hits[0].score ({hits[0].score}) should be >= hits[1].score ({hits[1].score})"
        )

    def test_no_match(self, make_db: sqlite3.Connection) -> None:
        _open_with_data(make_db, [_make_item("hello world", "more text")])
        s = SearcherImpl(make_db)
        hits = s.search("nonexistent", limit=5)
        assert hits == []

    def test_empty_query(self, make_db: sqlite3.Connection) -> None:
        _open_with_data(make_db, [_make_item("hello", "world")])
        s = SearcherImpl(make_db)
        # Empty / whitespace-only queries must return [] without erroring.
        assert s.search("", limit=5) == []
        assert s.search("   ", limit=5) == []

    def test_injection_safety(self, make_db: sqlite3.Connection) -> None:
        # fts_query_string must escape embedded quotes so user input
        # can't inject FTS5 operators (AND/OR/NEAR/^/column filters).
        _open_with_data(make_db, [_make_item("foo", "bar")])
        s = SearcherImpl(make_db)
        # Embedded quote — should be doubled, making this a search for
        # the literal phrase `foo" OR 1=1` (which won't match anything).
        hits = s.search('foo" OR 1=1', limit=5)
        assert hits == []

    def test_long_query_still_uses_fts(self, make_db: sqlite3.Connection) -> None:
        # Regression guard: 3+ rune queries must go through FTS path,
        # not LIKE. FTS path produces a non-empty snippet; LIKE leaves
        # the snippet empty.
        _open_with_data(make_db, [_make_item("部署文档", "如何部署详细")])
        s = SearcherImpl(make_db)
        hits = s.search("部署文", limit=5)
        assert len(hits) == 1
        assert hits[0].snippet != "", (
            "3-char query must use FTS path (snippet non-empty); "
            f"got snippet={hits[0].snippet!r}"
        )


class TestExternalizedContentRegression:
    """CRITICAL: externalized content must not corrupt FTS.

    Port of Go's ``TestSearcher_FTS_ExternalizedContentDoesNotCorrupt``.

    When content is externalized post-INSERT (context_item.content is
    empty but context_fts's inverted index retains the tokens because
    ReindexFTS rewrote the row directly, bypassing the AFTER UPDATE
    trigger), FTS5's snippet(context_fts, 2, ...) call detects the
    divergence between the inverted index and the external content
    table and returns SQLITE_CORRUPT_VTAB, surfaced by SQLite as
    "database disk image is malformed". This MUST NOT abort the whole
    search — the content snippet is dropped from the SQL so the
    MATCH+JOIN path returns hits cleanly. See searcher.go:searchSQL.
    """

    def test_externalized_content_does_not_corrupt(self, make_db: sqlite3.Connection) -> None:
        # Empty content simulates post-externalization state (FileStore
        # has the bytes; context_item.content is empty).
        item = _make_item("Composer Paper", "")
        repo = _open_with_data(make_db, [item])

        # Simulate ReindexFTS as IngestService does after externalizing:
        # write the real content directly to context_fts, bypassing
        # triggers.
        repo.reindex_fts(item.id, item.title, "", "Batchsize tuning convergence")

        # Sanity: confirm the divergence actually exists — the FTS
        # inverted index contains a token that context_item.content does
        # not (because content is "").
        n = make_db.execute(
            "SELECT count(*) FROM context_fts WHERE context_fts MATCH 'batchsize'"
        ).fetchone()[0]
        assert n == 1, "test setup: FTS index must contain the token"

        s = SearcherImpl(make_db)
        # This MUST NOT raise "database disk image is malformed".
        hits = s.search("Batchsize", limit=5)
        assert len(hits) == 1, "externalized content must still be findable via FTS MATCH"
        # Title snippet still works (title is inline in context_item).
        assert "Composer" in hits[0].snippet, (
            "title snippet must still be returned for externalized rows; "
            f"got {hits[0].snippet!r}"
        )


# ---------------------------------------------------------------------------
# Integration tests — LIKE fallback path.
# ---------------------------------------------------------------------------


class TestSearchLikeFallback:
    """LIKE fallback for queries < 3 code points."""

    def test_short_cjk_query_uses_like(self, make_db: sqlite3.Connection) -> None:
        # 2-char CJK queries are below the trigram minimum and would
        # silently return 0 results without the LIKE fallback.
        _open_with_data(
            make_db,
            [
                _make_item("部署", "上线流程"),  # title contains "部署"
                _make_item("上线", "gunicorn 部署 nginx"),  # content contains "部署"
                _make_item("无关", "与目标无任何关系"),  # neither
            ],
        )
        s = SearcherImpl(make_db)
        hits = s.search("部署", limit=10)
        assert len(hits) == 2, "LIKE fallback must match title and content for 2-char CJK"

    def test_short_ascii_query_uses_like(self, make_db: sqlite3.Connection) -> None:
        # Same fallback for ASCII queries shorter than 3 chars.
        _open_with_data(
            make_db,
            [
                _make_item("golang notes", "rust comparison"),
                _make_item("rust notes", "go vs rust"),
                _make_item("python", "java"),
            ],
        )
        s = SearcherImpl(make_db)
        hits = s.search("go", limit=10)
        assert len(hits) == 2, "LIKE fallback must match case-insensitively for 2-char ASCII"

    def test_like_escapes_wildcards(self, make_db: sqlite3.Connection) -> None:
        # User input containing LIKE wildcards (%, _) must be escaped
        # so they match literally rather than acting as wildcards.
        _open_with_data(
            make_db,
            [
                _make_item("foo", "100% done"),  # contains literal %
                _make_item("bar", "10_percent"),  # contains literal _
                _make_item("baz", "everything else"),  # no wildcards
            ],
        )
        s = SearcherImpl(make_db)

        hits = s.search("%", limit=10)
        assert len(hits) == 1, "literal % should only match items containing %, not everything"
        # Verify the matched row is the one with literal %.
        matched_content = _get_content(make_db, hits[0].id)
        assert matched_content == "100% done"

        hits = s.search("_", limit=10)
        assert len(hits) == 1, "literal _ should only match items containing _, not everything"
        matched_content = _get_content(make_db, hits[0].id)
        assert matched_content == "10_percent"


def _get_content(db: sqlite3.Connection, id: str) -> str:
    """Fetch the content column for an item id — used by LIKE tests to
    assert which row matched. Equivalent of Go's ``getHitContent``.
    """
    # Bypass row_factory (we want a raw scalar, not a ContextItem).
    row = db.execute("SELECT content FROM context_item WHERE id = ?", (id,)).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Integration tests — clamp_limit end-to-end on both paths.
# ---------------------------------------------------------------------------


class TestClampLimitRegression:
    """clamp_limit regression at the search() entry point (commit 4d26cea).

    Same regression as on the Go side: service-layer over-fetch
    (search.go overFetch = limit*3) passes Limit=300 for a user-requested
    limit=100. The buggy conditional reset that to 20; the fix clamps to
    200. We index 30 items that all share a matchable substring and
    verify all 30 are returned (buggy code would return 20).
    """

    def test_fts_limit_above_200_clamped_not_reset(self, make_db: sqlite3.Connection) -> None:
        items: list[ContextItem] = []
        for _ in range(30):
            # "部署 部署" is well above the trigram minimum and every
            # item carries the same phrase so FTS finds all 30.
            items.append(_make_item("部署 部署", "部署 shared"))
        _open_with_data(make_db, items)
        s = SearcherImpl(make_db)

        hits = s.search("部署 部署", limit=300)
        assert len(hits) == 30, (
            "FTS Limit=300 must clamp to 200 (not reset to 20) and return "
            f"all 30 matches; got {len(hits)}"
        )

    def test_like_limit_above_200_clamped_not_reset(self, make_db: sqlite3.Connection) -> None:
        items: list[ContextItem] = []
        for _ in range(30):
            # "部署" is 2 chars (6 UTF-8 bytes) -> triggers LIKE fallback.
            items.append(_make_item("部署", "shared content"))
        _open_with_data(make_db, items)
        s = SearcherImpl(make_db)

        hits = s.search("部署", limit=300)
        assert len(hits) == 30, (
            "LIKE Limit=300 must clamp to 200 (not reset to 20) and return "
            f"all 30 matches; got {len(hits)}"
        )


# ---------------------------------------------------------------------------
# SearchHit dataclass shape.
# ---------------------------------------------------------------------------


class TestSearchHitShape:
    """SearchHit dataclass has the expected fields."""

    def test_fields(self) -> None:
        h = SearchHit(id="abc", score=1.5, snippet="...")
        assert h.id == "abc"
        assert h.score == 1.5
        assert h.snippet == "..."
