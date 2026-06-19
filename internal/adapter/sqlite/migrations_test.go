package sqlite

import (
	"database/sql"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestMigrations_RunOnFreshDB(t *testing.T) {
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	defer db.Close()

	require.NoError(t, Migrate(db))

	var version string
	err = db.QueryRow(`SELECT value FROM schema_meta WHERE key='schema_version'`).Scan(&version)
	require.NoError(t, err)
	assert.Equal(t, "1", version)

	// Tables exist
	for _, table := range []string{"context_item", "context_fts", "project", "schema_meta"} {
		var name string
		err = db.QueryRow(
			`SELECT name FROM sqlite_master WHERE type='table' AND name=?`, table,
		).Scan(&name)
		require.NoError(t, err, "table %s should exist", table)
	}
}

func TestMigrations_Idempotent(t *testing.T) {
	db, _ := sql.Open("sqlite3", ":memory:")
	defer db.Close()

	require.NoError(t, Migrate(db))
	require.NoError(t, Migrate(db)) // second run is a no-op
}

func TestMigrations_0002_CreatesEmbeddingTables(t *testing.T) {
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	require.NoError(t, Migrate(db))

	// embedding_model seeded with default bge-m3
	var slug string
	var isDefault int
	err = db.QueryRow(`SELECT slug, is_default FROM embedding_model WHERE slug='bge-m3'`).Scan(&slug, &isDefault)
	require.NoError(t, err)
	assert.Equal(t, "bge-m3", slug)
	assert.Equal(t, 1, isDefault, "bge-m3 should be the default model")

	// context_embedding table exists
	_, err = db.Exec(`INSERT INTO context_embedding (item_id, model_slug, embedded_at, status) VALUES ('test', 'bge-m3', 0, 'done')`)
	// Will fail FK if context_item doesn't have 'test' row, but we only
	// care that the table exists. Use a no-op check.
	assert.NoError(t, err) // sqlite FK enforcement is deferred by default;
	// if this fails, the table is missing or FK is misconfigured.

	// Cleanup the test row to keep :memory: clean for subsequent checks
	_, _ = db.Exec(`DELETE FROM context_embedding WHERE item_id='test'`)

	// vec0 virtual table queryable
	var n int
	err = db.QueryRow(`SELECT count(*) FROM vec_bge_m3_1024`).Scan(&n)
	assert.NoError(t, err, "vec_bge_m3_1024 must exist and be queryable")
}

func TestMigrations_0002_IdempotentFromFreshDB(t *testing.T) {
	// Migrate twice — second run should be a no-op (version check).
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })
	require.NoError(t, Migrate(db))
	require.NoError(t, Migrate(db), "second Migrate call must be no-op")

	// Still exactly one default model
	var n int
	require.NoError(t, db.QueryRow(`SELECT count(*) FROM embedding_model WHERE is_default=1`).Scan(&n))
	assert.Equal(t, 1, n)
}
