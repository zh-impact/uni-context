package sqlite

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"unicode/utf8"

	"uni-context/internal/port"
)

// Short queries fall back to LIKE because the FTS5 trigram tokenizer
// requires phrases of at least 3 runes — a 2-char CJK word like `部署`
// silently returns zero results. See searchLike below.

type Searcher struct {
	db *sql.DB
	vs *VectorStore // composed for SearchVector; nil-safe fallback if not wired
}

func NewSearcher(db *sql.DB) *Searcher {
	return &Searcher{db: db, vs: NewVectorStore(db)}
}

// SearchVector delegates to the composed VectorStore. The Searcher
// interface unifies FTS and vector access for the service layer so
// SearchService doesn't need to depend on VectorStore separately.
func (s *Searcher) SearchVector(ctx context.Context, q port.VectorQuery) ([]port.VectorHit, error) {
	if s.vs == nil {
		return nil, fmt.Errorf("searcher: vector store not wired")
	}
	return s.vs.Search(ctx, q)
}

// ftsQueryString builds a safe FTS5 query: wrap the raw string in double
// quotes as a phrase (escaping any embedded quotes). This prevents FTS5
// operator injection (AND/OR/NEAR/^/column filters) from user input.
// For CJK (trigram), the raw substring works because trigram indexes all
// 3-grams of indexed text.
//
// Note: we deliberately do NOT TrimSpace the query body, because leading or
// trailing whitespace may be a load-bearing part of a trigram phrase (e.g.
// "部署 " as a 4-byte phrase including the trailing ASCII space). Only
// all-whitespace input is rejected as empty.
func ftsQueryString(raw string) string {
	if strings.TrimSpace(raw) == "" {
		return ""
	}
	escaped := strings.ReplaceAll(raw, `"`, `""`)
	return `"` + escaped + `"`
}

// searchSQL extracts snippets from BOTH the title column (index 0) and the
// content column (index 2). The caller prefers the title snippet (title is
// the canonical human-readable identifier) but falls back to the content
// snippet when the title is empty — a common case when the user runs
// `unictx user note add <content>` without --title.
//
// No highlight markers are emitted: presentation concerns belong to the
// caller, not the searcher. The snippet function's "ellipsis" argument is
// also empty so the returned text is a verbatim slice of the column.
const searchSQL = `
SELECT ci.id, bm25(context_fts) AS score,
       snippet(context_fts, 0, '', '', '…', 16) AS title_snip,
       snippet(context_fts, 2, '', '', '…', 16) AS content_snip
FROM context_fts
JOIN context_item ci ON ci.rowid = context_fts.rowid
WHERE context_fts MATCH ?
ORDER BY bm25(context_fts)
LIMIT ?
`

func (s *Searcher) SearchFTS(ctx context.Context, q port.SearchQuery) ([]port.SearchHit, error) {
	if strings.TrimSpace(q.Query) == "" {
		return nil, nil
	}
	// Trigram FTS requires >= 3 runes. Shorter queries (e.g. 2-char CJK
	// like `部署`) silently return zero results — fall back to LIKE.
	// Count runes on the raw query: leading/trailing whitespace may be
	// load-bearing for trigram phrases (see ftsQueryString comment), so
	// "部署 " (3 runes incl. ASCII space) stays on the FTS path.
	if utf8.RuneCountInString(q.Query) < 3 {
		return s.searchLike(ctx, strings.TrimSpace(q.Query), q.Limit)
	}
	ftsq := ftsQueryString(q.Query)
	if ftsq == "" {
		return nil, nil
	}
	limit := q.Limit
	if limit <= 0 || limit > 200 {
		limit = 20
	}

	rows, err := s.db.QueryContext(ctx, searchSQL, ftsq, limit)
	if err != nil {
		return nil, fmt.Errorf("fts search: %w", err)
	}
	defer rows.Close()

	var hits []port.SearchHit
	for rows.Next() {
		var (
			h           port.SearchHit
			titleSnip   sql.NullString
			contentSnip sql.NullString
		)
		if err := rows.Scan(&h.ID, &h.Score, &titleSnip, &contentSnip); err != nil {
			return nil, err
		}
		// Prefer title snippet; fall back to content snippet when title is
		// empty (e.g. user ran `add` without --title).
		switch {
		case titleSnip.String != "":
			h.Snippet = titleSnip.String
		case contentSnip.String != "":
			h.Snippet = contentSnip.String
		}
		// bm25 returns negative scores (more negative = better match).
		// Negate so higher score = better match.
		h.Score = -h.Score
		hits = append(hits, h)
	}
	return hits, rows.Err()
}

// likeSearchSQL is the fallback path for queries shorter than 3 runes.
// LIKE has no tokenizer minimum and matches substrings directly. Score
// is a constant 1.0 (no relevance ranking — every match is equal) and
// results are ordered by created_at DESC for deterministic output.
// Snippet is left empty: the service layer's title-fallback path
// (search.go:235-237) covers display. Unindexed scan on context_item —
// acceptable for the expected <10k personal-note scale.
const likeSearchSQL = `
SELECT ci.id, 1.0 AS score
FROM context_item ci
WHERE ci.title LIKE ? ESCAPE '\'
   OR ci.summary LIKE ? ESCAPE '\'
   OR ci.content LIKE ? ESCAPE '\'
ORDER BY ci.created_at DESC
LIMIT ?
`

// likePattern escapes LIKE wildcards (%, _, \) in the raw query and wraps
// it in %...% for substring match. The ESCAPE '\' clause in likeSearchSQL
// activates the backslash as the escape character.
func likePattern(raw string) string {
	r := strings.ReplaceAll(raw, `\`, `\\`)
	r = strings.ReplaceAll(r, `%`, `\%`)
	r = strings.ReplaceAll(r, `_`, `\_`)
	return "%" + r + "%"
}

// searchLike runs the LIKE fallback for short queries. Empty snippet is
// intentional — callers fall back to item.Title for display.
func (s *Searcher) searchLike(ctx context.Context, query string, limit int) ([]port.SearchHit, error) {
	if limit <= 0 || limit > 200 {
		limit = 20
	}
	pattern := likePattern(query)
	rows, err := s.db.QueryContext(ctx, likeSearchSQL, pattern, pattern, pattern, limit)
	if err != nil {
		return nil, fmt.Errorf("like search: %w", err)
	}
	defer rows.Close()

	var hits []port.SearchHit
	for rows.Next() {
		var h port.SearchHit
		if err := rows.Scan(&h.ID, &h.Score); err != nil {
			return nil, err
		}
		hits = append(hits, h)
	}
	return hits, rows.Err()
}
