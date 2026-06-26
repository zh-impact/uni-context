package sqlite

import (
	"context"
	"database/sql"
	"testing"

	"uni-context/internal/domain"
	"uni-context/internal/port"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// NOTE on deviations from the brief:
//
// The brief's tests as written cannot pass against the schema delivered in
// Task 4 for two reasons, both of which were anticipated by the brief's
// "Heads-up" section. The deviations below are the minimum changes needed
// to make the tests valid against the searcher contract.
//
// 1) Trigram minimum-token length. The FTS5 trigram tokenizer
//    (https://www.sqlite.org/fts5.html) indexes every contiguous
//    3-character sequence and cannot match query phrases shorter than 3
//    characters. The brief's tests all use the 2-character CJK query
//    "部署", which therefore returns zero rows regardless of searcher
//    correctness. We've replaced those queries with 3+ character CJK
//    substrings drawn from the same sample data and exercising the same
//    searcher behaviors (multi-result match, single-result CJK substring
//    match, BM25 ranking, no-match, empty query, operator-injection
//    safety).
//
// 2) Snippet source column. The brief's implementation used
//    `snippet(context_fts, 2, ...)` which extracts from the `content`
//    column (column index 2), but its BasicMatch assertion compared the
//    snippet against title text ("如何部署 Go 服务到 k8s"). For the test
//    data the match lives in the title, so a content snippet cannot
//    contain the matched term. The snippet is now extracted from the
//    title (column index 0), which is the canonical human-readable
//    identifier for a context_item and is where users typically search.
//    This matches what port.SearchHit.Snippet implies (an opaque context
//    string — no column is dictated by the contract).

func TestSearcher_FTS_BasicMatch(t *testing.T) {
	db := openMemWithSampleData(t, []domain.ContextItem{
		makeItem("如何部署 Go 服务到 k8s", "k8s deployment yaml 示例"),
		makeItem("向量数据库选型对比", "Qdrant vs sqlite-vec"),
		makeItem("Python 部署 Flask 应用", "gunicorn + nginx"),
	})
	s := NewSearcher(db)

	// "部署 " (部署 + ASCII space) is 4 bytes, appears in both titles.
	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "部署 ", Limit: 10})
	require.NoError(t, err)
	require.Len(t, hits, 2, "both items whose title contains '部署 ' should match")

	// Both matched snippets must be one of the two expected titles (order
	// independent — BM25 may rank them in either order depending on term
	// frequency). The brief's original assertion concatenated the two
	// snippets without a separator and used assert.Contains against the
	// individual titles, which can never be satisfied for two non-empty
	// strings; we assert set equality instead, which is what the brief
	// was clearly after.
	expected := map[string]bool{
		"如何部署 Go 服务到 k8s":    true,
		"Python 部署 Flask 应用": true,
	}
	for _, h := range hits {
		assert.True(t, expected[h.Snippet],
			"unexpected snippet %q (id=%s, score=%v)", h.Snippet, h.ID, h.Score)
	}
}

func TestSearcher_FTS_CJKTrigram(t *testing.T) {
	// 3-character CJK query — minimum the trigram tokenizer can match.
	db := openMemWithSampleData(t, []domain.ContextItem{
		makeItem("部署文档", "如何部署"),
		makeItem("上线流程", "与部署无关"),
	})
	s := NewSearcher(db)

	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "部署文", Limit: 5})
	require.NoError(t, err)
	assert.GreaterOrEqual(t, len(hits), 1)
}

func TestSearcher_FTS_RankingBM25(t *testing.T) {
	// Item A has the 3-char query "部署 A" repeated many times in both title
	// and content; item B has it once. BM25 should rank A first.
	db := openMemWithSampleData(t, []domain.ContextItem{
		makeItem("部署 A 部署 A 部署 A", "部署 A部署 A部署 A"),
		makeItem("部署 A", "无关内容"),
	})
	s := NewSearcher(db)
	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "部署 A", Limit: 5})
	require.NoError(t, err)
	require.GreaterOrEqual(t, len(hits), 2)
	// Higher-frequency match should rank first. bm25() returns negative
	// scores (more negative = better); we negate so higher = better.
	assert.True(t, hits[0].Score >= hits[1].Score,
		"hits[0].Score (%v) should be >= hits[1].Score (%v)", hits[0].Score, hits[1].Score)
}

func TestSearcher_FTS_NoMatch(t *testing.T) {
	db := openMemWithSampleData(t, []domain.ContextItem{makeItem("hello world", "more text")})
	s := NewSearcher(db)
	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "nonexistent", Limit: 5})
	require.NoError(t, err)
	assert.Empty(t, hits)
}

func TestSearcher_FTS_EmptyQuery(t *testing.T) {
	db := openMemWithSampleData(t, []domain.ContextItem{makeItem("hello", "world")})
	s := NewSearcher(db)
	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "", Limit: 5})
	require.NoError(t, err)
	assert.Empty(t, hits)
}

func TestSearcher_FTS_QueryInjectionSafety(t *testing.T) {
	// ftsQueryString must escape embedded quotes so user input can't inject
	// FTS5 operators (AND/OR/NEAR/^/column filters).
	db := openMemWithSampleData(t, []domain.ContextItem{makeItem("foo", "bar")})
	s := NewSearcher(db)

	// Embedded quote — should be doubled, making this a search for the
	// literal phrase `foo" OR 1=1` (which won't match anything).
	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: `foo" OR 1=1`, Limit: 5})
	require.NoError(t, err)
	assert.Empty(t, hits, "operator injection should be neutralized by quote escaping")
}

// TestSearcher_FTS_TitleLessNote_MatchesViaContent_NoSnippet: title-less
// notes still match via content tokens (FTS index spans all columns), but
// the snippet is empty because we no longer extract a content-column
// snippet. See searchSQL comment for why content snippets were dropped.
// Callers fall back to item.Title in the display layer; for title-less
// notes that's also empty, so the CLI shows just the score. This is the
// trade-off for fixing the malformed-FTS error on externalized content.
func TestSearcher_FTS_TitleLessNote_MatchesViaContent_NoSnippet(t *testing.T) {
	db := openMemWithSampleData(t, []domain.ContextItem{
		makeItem("", "important content about deployment here"),
	})
	s := NewSearcher(db)

	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "important content", Limit: 5})
	require.NoError(t, err)
	require.Len(t, hits, 1, "title-less note should still be findable via content match")
	assert.Empty(t, hits[0].Snippet,
		"snippet must be empty when title is empty; content snippets were dropped (got %q)", hits[0].Snippet)
}

// TestSearcher_FTS_ExternalizedContentDoesNotCorrupt: regression guard
// for the malformed-FTS bug. When content is externalized post-INSERT
// (content_item.content is empty but context_fts's inverted index retains
// the tokens because ReindexFTS rewrote the row directly, bypassing the
// AFTER UPDATE trigger), FTS5's snippet(context_fts, 2, ...) call detects
// the divergence between the inverted index and the external content
// table and returns SQLITE_CORRUPT_VTAB, surfaced by SQLite as "database
// disk image is malformed". This MUST NOT abort the whole search — the
// content snippet is dropped from the SQL so the MATCH+JOIN path returns
// hits cleanly. See searcher.go:searchSQL.
func TestSearcher_FTS_ExternalizedContentDoesNotCorrupt(t *testing.T) {
	item := makeItem("Composer Paper", "") // empty content simulates post-externalization
	db := openMemWithSampleData(t, []domain.ContextItem{item})
	repo := NewContextRepo(db)
	// Simulate ReindexFTS as IngestService.Create does after externalizing:
	// write the real content directly to context_fts, bypassing triggers.
	require.NoError(t, repo.ReindexFTS(context.Background(),
		item.ID, item.Title, "", "Batchsize tuning convergence"))

	// Sanity: confirm the divergence actually exists.
	var n int
	require.NoError(t, db.QueryRow(`SELECT count(*) FROM context_fts WHERE context_fts MATCH 'batchsize'`).Scan(&n))
	require.Equal(t, 1, n, "test setup: FTS index must contain the token")

	s := NewSearcher(db)
	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "Batchsize", Limit: 5})
	require.NoError(t, err, "search must not return 'database disk image is malformed' for externalized content")
	require.Len(t, hits, 1, "externalized content must still be findable via FTS MATCH")
	// Title snippet still works (title is inline in context_item).
	assert.Contains(t, hits[0].Snippet, "Composer",
		"title snippet must still be returned for externalized rows; got %q", hits[0].Snippet)
}

// TestSearcher_FTS_LikeFallback_ShortCJKQuery: 2-char CJK queries are
// shorter than the trigram minimum and would silently return 0 results.
// The LIKE fallback must find substring matches in both title and content.
func TestSearcher_FTS_LikeFallback_ShortCJKQuery(t *testing.T) {
	db := openMemWithSampleData(t, []domain.ContextItem{
		makeItem("部署", "上线流程"),              // title contains "部署"
		makeItem("上线", "gunicorn 部署 nginx"), // content contains "部署"
		makeItem("无关", "与目标无任何关系"),          // neither
	})
	s := NewSearcher(db)

	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "部署", Limit: 10})
	require.NoError(t, err)
	require.Len(t, hits, 2, "LIKE fallback must match title and content for 2-char CJK")
}

// TestSearcher_FTS_LikeFallback_ShortASCIIQuery: same fallback for ASCII
// queries shorter than 3 chars (e.g. "go" matches "golang").
func TestSearcher_FTS_LikeFallback_ShortASCIIQuery(t *testing.T) {
	db := openMemWithSampleData(t, []domain.ContextItem{
		makeItem("golang notes", "rust comparison"),
		makeItem("rust notes", "go vs rust"),
		makeItem("python", "java"),
	})
	s := NewSearcher(db)

	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "go", Limit: 10})
	require.NoError(t, err)
	require.Len(t, hits, 2, "LIKE fallback must match case-insensitively for 2-char ASCII")
}

// TestSearcher_FTS_LikeFallback_EscapesWildcards: user input containing
// LIKE wildcards (%, _) must be escaped so they match literally rather
// than acting as wildcards.
func TestSearcher_FTS_LikeFallback_EscapesWildcards(t *testing.T) {
	db := openMemWithSampleData(t, []domain.ContextItem{
		makeItem("foo", "100% done"),       // contains literal %
		makeItem("bar", "10_percent"),      // contains literal _
		makeItem("baz", "everything else"), // no wildcards
	})
	s := NewSearcher(db)

	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "%", Limit: 10})
	require.NoError(t, err)
	require.Len(t, hits, 1, "literal % should only match items containing %, not everything")
	assert.Equal(t, "100% done", getHitContent(t, db, hits[0].ID))

	hits, err = s.SearchFTS(context.Background(), port.SearchQuery{Query: "_", Limit: 10})
	require.NoError(t, err)
	require.Len(t, hits, 1, "literal _ should only match items containing _, not everything")
	assert.Equal(t, "10_percent", getHitContent(t, db, hits[0].ID))
}

// TestSearcher_FTS_LongQueryStillUsesFTS: 3+ char queries must still go
// through the FTS path (BM25 ranking, snippet extraction). Regression
// guard against the LIKE fallback accidentally swallowing longer queries.
func TestSearcher_FTS_LongQueryStillUsesFTS(t *testing.T) {
	db := openMemWithSampleData(t, []domain.ContextItem{
		makeItem("部署文档", "如何部署详细"),
	})
	s := NewSearcher(db)

	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "部署文", Limit: 5})
	require.NoError(t, err)
	require.Len(t, hits, 1)
	// FTS path produces a snippet; LIKE path leaves snippet empty.
	// Non-empty snippet proves we hit the FTS path.
	assert.NotEmpty(t, hits[0].Snippet, "3-char query must use FTS path (snippet non-empty)")
}

// TestSearcher_FTS_LimitAbove200ClampedNotReset: same regression as
// TestVectorStore_Search_LimitAbove200ClampedNotReset but on the FTS
// path. The service-layer over-fetch (search.go overFetch = limit*3)
// passes Limit=300 for a user-requested limit=100. The buggy
// conditional reset that to 20; the fix clamps to 200. We index 30
// items that all share a 3-char FTS-matchable substring and verify all
// 30 are returned (buggy code would return 20).
func TestSearcher_FTS_LimitAbove200ClampedNotReset(t *testing.T) {
	items := make([]domain.ContextItem, 0, 30)
	for range 30 {
		// "部署 X" is 4 runes — well above the trigram minimum — and
		// every item carries the same 3-char prefix "部署 " so the
		// FTS phrase match finds all 30.
		items = append(items, makeItem("部署 部署", "部署 shared"))
	}
	db := openMemWithSampleData(t, items)
	s := NewSearcher(db)

	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "部署 ", Limit: 300})
	require.NoError(t, err)
	assert.Len(t, hits, 30,
		"FTS Limit=300 must clamp to 200 (not reset to 20) and return all 30 matches; got %d", len(hits))
}

// TestSearcher_LikeFallback_LimitAbove200ClampedNotReset: same
// regression guard on the LIKE fallback path (2-char CJK queries that
// fall below the trigram minimum).
func TestSearcher_LikeFallback_LimitAbove200ClampedNotReset(t *testing.T) {
	items := make([]domain.ContextItem, 0, 30)
	for range 30 {
		// "部署" is 2 chars (6 UTF-8 bytes) -> triggers LIKE fallback.
		items = append(items, makeItem("部署", "shared content"))
	}
	db := openMemWithSampleData(t, items)
	s := NewSearcher(db)

	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "部署", Limit: 300})
	require.NoError(t, err)
	assert.Len(t, hits, 30,
		"LIKE Limit=300 must clamp to 200 (not reset to 20) and return all 30 matches; got %d", len(hits))
}

// getHitContent fetches the content column for an item ID. Used by LIKE
// fallback tests to assert which row matched.
func getHitContent(t *testing.T, db *sql.DB, id string) string {
	t.Helper()
	var content string
	err := db.QueryRow(`SELECT content FROM context_item WHERE id = ?`, id).Scan(&content)
	require.NoError(t, err)
	return content
}

func makeItem(title, content string) domain.ContextItem {
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = title
	item.Content = content
	return item
}

func openMemWithSampleData(t *testing.T, items []domain.ContextItem) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	require.NoError(t, Migrate(db))
	t.Cleanup(func() { db.Close() })
	repo := NewContextRepo(db)
	for _, it := range items {
		require.NoError(t, repo.Create(context.Background(), it))
	}
	return db
}
