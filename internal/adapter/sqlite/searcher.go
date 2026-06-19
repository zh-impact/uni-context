package sqlite

import (
	"context"
	"database/sql"
	"fmt"
	"strings"

	"uni-context/internal/port"
)

type Searcher struct {
	db *sql.DB
}

func NewSearcher(db *sql.DB) *Searcher {
	return &Searcher{db: db}
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

// searchSQL extracts the snippet from column index 0 (title) of the FTS5
// virtual table. Title is the canonical human-readable identifier of a
// context_item and is where users typically search from. Using content
// (column 2) would frequently return a snippet that does not contain the
// matched term when the match lives in the title.
//
// No highlight markers are emitted: presentation concerns belong to the
// caller, not the searcher. The snippet function's "ellipsis" argument is
// also empty so the returned text is a verbatim slice of the column.
const searchSQL = `
SELECT ci.id, bm25(context_fts) AS score,
       snippet(context_fts, 0, '', '', '…', 16) AS snip
FROM context_fts
JOIN context_item ci ON ci.rowid = context_fts.rowid
WHERE context_fts MATCH ?
ORDER BY bm25(context_fts)
LIMIT ?
`

func (s *Searcher) SearchFTS(ctx context.Context, q port.SearchQuery) ([]port.SearchHit, error) {
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
		var h port.SearchHit
		if err := rows.Scan(&h.ID, &h.Score, &h.Snippet); err != nil {
			return nil, err
		}
		// bm25 returns negative scores (more negative = better match).
		// Negate so higher score = better match.
		h.Score = -h.Score
		hits = append(hits, h)
	}
	return hits, rows.Err()
}
