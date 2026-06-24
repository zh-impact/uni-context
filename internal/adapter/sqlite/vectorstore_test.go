package sqlite

import (
	"context"
	"testing"

	"uni-context/internal/domain"
	"uni-context/internal/port"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func newVectorStoreFixture(t *testing.T) (*VectorStore, port.ContextRepo) {
	t.Helper()
	db := openMemWithSampleData(t, nil) // from searcher_test.go
	repo := NewContextRepo(db)
	vs := NewVectorStore(db)
	return vs, repo
}

func putItem(t *testing.T, repo port.ContextRepo, title string) string {
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = title
	require.NoError(t, repo.Create(context.Background(), item))
	return item.ID
}

// vec1024 returns a 1024-dim sparse vector with the given (index,value)
// pairs set; all other entries are zero. Plan 2a's vec0 table is
// hardcoded FLOAT[1024], so tests must use 1024-dim vectors — but the
// orthogonality properties of the original 4-dim design (one-hot +
// a small perturbation in the query) carry over unchanged.
func vec1024(set ...struct {
	idx int
	val float32
}) []float32 {
	v := make([]float32, 1024)
	for _, s := range set {
		v[s.idx] = s.val
	}
	return v
}

func TestVectorStore_PutAndSearch_KNN(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()

	id1 := putItem(t, repo, "go deployment")
	id2 := putItem(t, repo, "python scraping")
	id3 := putItem(t, repo, "rust async")

	// One-hot vectors: id1≈e_0, id2≈e_1, id3≈e_2. The query is e_0 with
	// a small e_1 component, so id1 is closest by cosine distance.
	require.NoError(t, vs.Put(ctx, "bge-m3", id1, vec1024(
		struct {
			idx int
			val float32
		}{0, 1.0},
	)))
	require.NoError(t, vs.Put(ctx, "bge-m3", id2, vec1024(
		struct {
			idx int
			val float32
		}{1, 1.0},
	)))
	require.NoError(t, vs.Put(ctx, "bge-m3", id3, vec1024(
		struct {
			idx int
			val float32
		}{2, 1.0},
	)))

	hits, err := vs.Search(ctx, port.VectorQuery{
		Vector: vec1024(
			struct {
				idx int
				val float32
			}{0, 1.0},
			struct {
				idx int
				val float32
			}{1, 0.1},
		),
		Model: "bge-m3",
		Limit: 3,
	})
	require.NoError(t, err)
	require.Len(t, hits, 3, "all 3 items should be returned")
	assert.Equal(t, id1, hits[0].ID, "closest to query should be id1")
}

func TestVectorStore_PutIsIdempotent(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()
	id := putItem(t, repo, "title")

	vec := vec1024(struct {
		idx int
		val float32
	}{0, 1.0})
	require.NoError(t, vs.Put(ctx, "bge-m3", id, vec))
	require.NoError(t, vs.Put(ctx, "bge-m3", id, vec), "second Put with same value should succeed")

	hits, err := vs.Search(ctx, port.VectorQuery{Vector: vec, Model: "bge-m3", Limit: 5})
	require.NoError(t, err)
	require.Len(t, hits, 1, "idempotent Put must not duplicate")
}

func TestVectorStore_DeleteRemovesVector(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()
	id := putItem(t, repo, "title")
	vec := vec1024(struct {
		idx int
		val float32
	}{0, 1.0})
	require.NoError(t, vs.Put(ctx, "bge-m3", id, vec))

	require.NoError(t, vs.Delete(ctx, "bge-m3", id))
	hits, err := vs.Search(ctx, port.VectorQuery{Vector: vec, Model: "bge-m3", Limit: 5})
	require.NoError(t, err)
	assert.Empty(t, hits)
}

func TestVectorStore_SearchFiltersByScope(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()

	// Two items, same vector, different scopes
	userItem, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	userItem.Title = "user note"
	require.NoError(t, repo.Create(ctx, userItem))

	globalItem, _ := domain.NewContextItem(domain.ScopeGlobal, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{})
	globalItem.Title = "global note"
	require.NoError(t, repo.Create(ctx, globalItem))

	vec := vec1024(struct {
		idx int
		val float32
	}{0, 1.0})
	require.NoError(t, vs.Put(ctx, "bge-m3", userItem.ID, vec))
	require.NoError(t, vs.Put(ctx, "bge-m3", globalItem.ID, vec))

	hits, err := vs.Search(ctx, port.VectorQuery{
		Vector: vec, Model: "bge-m3", Limit: 10,
		Scopes: []string{"user"},
	})
	require.NoError(t, err)
	require.Len(t, hits, 1, "scope filter should narrow to user")
	assert.Equal(t, userItem.ID, hits[0].ID)
}

// bge-m3 is 1024-dim; this test exercises the real dimension end-to-end
// with a dense (non-sparse) vector.
func TestVectorStore_RealDimension(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()
	id := putItem(t, repo, "title")

	vec := make([]float32, 1024)
	for i := range vec {
		vec[i] = float32(i % 10)
	}
	require.NoError(t, vs.Put(ctx, "bge-m3", id, vec))

	hits, err := vs.Search(ctx, port.VectorQuery{Vector: vec, Model: "bge-m3", Limit: 1})
	require.NoError(t, err)
	require.Len(t, hits, 1)
	assert.Equal(t, id, hits[0].ID)
	assert.Greater(t, hits[0].Score, 0.0)
}

// TestVectorStore_Search_ReturnsAtMostLimit is the regression guard for
// the double over-fetch bug. VectorStore.Search used to multiply q.Limit
// by 3 internally ("fetchN := q.Limit * 3"), under the assumption that
// the caller would post-filter the results. But scope/kind filters are
// already pushed down to SQL via JOIN context_item — there is no
// post-filter inside VectorStore. The service layer (searchHybrid) is
// the correct place for over-fetch; it passes Limit=limit*3, and
// VectorStore must honor that verbatim.
//
// Without the fix: Limit=4 with 12 indexed items returned 12 hits
// (fetchN = 4*3 = 12). With the fix: returns 4 hits.
func TestVectorStore_Search_ReturnsAtMostLimit(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()

	// Put 12 items, each at a unique one-hot index so KNN can order them.
	// Querying with e_0 ranks item0 closest, items 1-11 equidistant
	// (orthogonal one-hot => same cosine distance from e_0).
	for i := range 12 {
		id := putItem(t, repo, "item")
		require.NoError(t, vs.Put(ctx, "bge-m3", id, vec1024(struct {
			idx int
			val float32
		}{i, 1.0})))
	}

	hits, err := vs.Search(ctx, port.VectorQuery{
		Vector: vec1024(struct {
			idx int
			val float32
		}{0, 1.0}),
		Model: "bge-m3",
		Limit: 4,
	})
	require.NoError(t, err)
	assert.Len(t, hits, 4,
		"VectorStore.Search must return at most q.Limit hits; got %d (double over-fetch bug?)", len(hits))
}

// TestVectorStore_Search_LimitAbove200ClampedNotReset is the regression
// guard for the limit-clamp bug. The service layer passes Limit=limit*3
// (search.go over-fetch); when the user requests limit=100, VectorStore
// sees q.Limit=300. The buggy conditional `if q.Limit > 200 { q.Limit = 20 }`
// reset this to 20 (catastrophic recall loss). The fix clamps to 200 —
// still well above the user's requested 100 — so all 30 indexed items
// are returned here. Buggy code would return only 20.
func TestVectorStore_Search_LimitAbove200ClampedNotReset(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()

	// 30 items: > 20 (buggy reset value) but < 200 (clamp ceiling), so
	// the test cleanly distinguishes reset-to-20 from clamp-to-200.
	for i := range 30 {
		id := putItem(t, repo, "item")
		require.NoError(t, vs.Put(ctx, "bge-m3", id, vec1024(struct {
			idx int
			val float32
		}{i, 1.0})))
	}

	hits, err := vs.Search(ctx, port.VectorQuery{
		Vector: vec1024(struct {
			idx int
			val float32
		}{0, 1.0}),
		Model: "bge-m3",
		Limit: 300, // service-layer over-fetch value
	})
	require.NoError(t, err)
	assert.Len(t, hits, 30,
		"Limit=300 must clamp to 200 (not reset to 20) and return all 30 indexed items; got %d", len(hits))
}

// TestVectorStore_Search_LimitZeroDefaultsTo20 verifies the <=0 branch
// still picks the default. After the fix, only the <=0 path sets 20 —
// positive values are never silently reset to 20.
func TestVectorStore_Search_LimitZeroDefaultsTo20(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()

	for i := range 30 {
		id := putItem(t, repo, "item")
		require.NoError(t, vs.Put(ctx, "bge-m3", id, vec1024(struct {
			idx int
			val float32
		}{i, 1.0})))
	}

	hits, err := vs.Search(ctx, port.VectorQuery{
		Vector: vec1024(struct {
			idx int
			val float32
		}{0, 1.0}),
		Model: "bge-m3",
		Limit: 0, // unset -> default 20
	})
	require.NoError(t, err)
	assert.Len(t, hits, 20,
		"Limit=0 must default to 20; got %d", len(hits))
}
