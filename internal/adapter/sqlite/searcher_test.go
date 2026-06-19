package sqlite

import (
	"context"
	"database/sql"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"uni-context/internal/domain"
	"uni-context/internal/port"
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
		"如何部署 Go 服务到 k8s":  true,
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

func TestSearcher_FTS_FallsBackToContentSnippet(t *testing.T) {
	// When a note has no title (title=""), the snippet extracted from the
	// title column is empty. The searcher must fall back to a content-column
	// snippet so search results still show useful context for title-less
	// notes (a common case when the user runs `unictx user note add foo`
	// without --title).
	db := openMemWithSampleData(t, []domain.ContextItem{
		makeItem("", "important content about deployment here"),
	})
	s := NewSearcher(db)

	hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "important content", Limit: 5})
	require.NoError(t, err)
	require.Len(t, hits, 1, "title-less note should still be findable via content match")
	assert.Contains(t, hits[0].Snippet, "important",
		"snippet must come from content column when title snippet is empty; got %q", hits[0].Snippet)
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
