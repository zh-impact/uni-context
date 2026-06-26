package sqlite

// defaultLimit is the limit applied when the caller passes <= 0 (unset).
// Matches the historical behavior of the inline `if limit <= 0 { limit = 20 }`.
const defaultLimit = 20

// maxLimit is the upper bound on returned rows. The vec0 KNN `k` parameter
// and the FTS/LIKE `LIMIT ?` all accept arbitrarily large values, but the
// service-layer over-fetch (search.go: overFetch = limit * 3) can push
// effective values well above the user's intent (limit=100 -> 300 here).
// Clamping (rather than resetting to defaultLimit) preserves the
// over-fetch headroom the caller asked for without unbounding the query.
const maxLimit = 200

// clampLimit normalizes a caller-supplied limit:
//   - n <= 0 returns defaultLimit (treats "unset" as the default).
//   - n > maxLimit returns maxLimit (preserves over-fetch headroom without
//     unbounding the query; previously this branch reset to defaultLimit,
//     which silently destroyed the caller's explicit over-fetch).
//   - otherwise returns n unchanged.
//
// Used by Searcher.SearchFTS, Searcher.searchLike, and VectorStore.Search
// so all three query paths apply identical limit semantics.
func clampLimit(n int) int {
	if n <= 0 {
		return defaultLimit
	}
	if n > maxLimit {
		return maxLimit
	}
	return n
}
