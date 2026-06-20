package sqlite

import (
	"context"
	"database/sql"
	"testing"

	"uni-context/internal/port"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// openTestDB gives each test a fresh migrated in-memory DB. The 0002
// migration seeds slug='bge-m3', dimension=1024, vec_table='vec_bge_m3_1024'.
func openTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })
	require.NoError(t, Migrate(db))
	return db
}

func TestEnsureModelRegistered_NoopForExistingSlug(t *testing.T) {
	db := openTestDB(t)
	// 'bge-m3' is seeded by 0002; second call should be a no-op.
	require.NoError(t, EnsureModelRegistered(db, "bge-m3", "ollama", 1024))

	var n int
	require.NoError(t, db.QueryRow(
		`SELECT COUNT(*) FROM embedding_model WHERE slug = 'bge-m3'`).Scan(&n))
	assert.Equal(t, 1, n, "seed row not duplicated")
}

func TestEnsureModelRegistered_RegistersAliasAtSameDimension(t *testing.T) {
	db := openTestDB(t)
	// Simulate LMStudio exposing bge-m3 as 'text-embedding-bge-m3'.
	require.NoError(t, EnsureModelRegistered(db,
		"text-embedding-bge-m3", "openai", 1024))

	var (
		vecTable  string
		isDefault int
		dim       int
	)
	require.NoError(t, db.QueryRow(
		`SELECT vec_table, is_default, dimension FROM embedding_model
		 WHERE slug = 'text-embedding-bge-m3'`).Scan(&vecTable, &isDefault, &dim))
	assert.Equal(t, "vec_bge_m3_1024", vecTable, "alias reuses seeded vec table")
	assert.Equal(t, 0, isDefault, "alias is not the default; seed row stays canonical")
	assert.Equal(t, 1024, dim)
}

func TestEnsureModelRegistered_RejectsUnknownDimension(t *testing.T) {
	db := openTestDB(t)
	// 768-dim (nomic-embed-text) has no seeded vec_table → Plan 2a rejects.
	err := EnsureModelRegistered(db, "nomic-embed-text", "openai", 768)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "dimension 768 not registered")
	assert.Contains(t, err.Error(), "Plan 2c")
}

func TestEnsureModelRegistered_VectorStoreAcceptsAliasSlug(t *testing.T) {
	// End-to-end: register an alias, then Put + Search should work
	// against the alias slug, all hitting the shared vec_bge_m3_1024 table.
	// Uses the same fixture pattern as vectorstore_test.go (repo.Create
	// first so the JOIN in VectorStore.Search finds the row).
	db := openTestDB(t)
	require.NoError(t, EnsureModelRegistered(db,
		"text-embedding-bge-m3", "openai", 1024))

	repo := NewContextRepo(db)
	vs := NewVectorStore(db)

	itemID := putItem(t, repo, "alias-target") // helper from vectorstore_test.go
	vec := vec1024(struct {                    // helper from vectorstore_test.go
		idx int
		val float32
	}{0, 1.0})

	require.NoError(t, vs.Put(context.Background(),
		"text-embedding-bge-m3", itemID, vec))

	hits, err := vs.Search(context.Background(), port.VectorQuery{
		Model: "text-embedding-bge-m3", Vector: vec, Limit: 5,
	})
	require.NoError(t, err)
	require.Len(t, hits, 1)
	assert.Equal(t, itemID, hits[0].ID)
}
