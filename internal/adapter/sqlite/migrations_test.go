package sqlite

import (
	"database/sql"
	"errors"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestWrapMigrationErr_FTS5MissingHint: when the underlying Exec error
// indicates SQLite was built without FTS5, the wrapped error must carry
// an actionable hint pointing at the build tag — so users running
// `go test ./...` without -tags sqlite_fts5 see "rebuild with
// -tags sqlite_fts5" instead of SQLite's bare "no such module: fts5".
func TestWrapMigrationErr_FTS5MissingHint(t *testing.T) {
	orig := errors.New("no such module: fts5")
	err := wrapMigrationErr("0001_init.sql", orig)
	require.Error(t, err)
	s := err.Error()
	assert.Contains(t, s, "0001_init.sql")
	assert.Contains(t, s, "sqlite_fts5", "must point at the build tag fix")
	assert.Contains(t, s, "no such module: fts5", "underlying error must remain wrapped/visible")
	assert.ErrorIs(t, err, orig, "errors.Is must still match the original")
}

// TestWrapMigrationErr_PlainErrorUnchanged: non-FTS5 errors pass through
// with the historical "exec migration <fname>" prefix and no FTS5 hint.
func TestWrapMigrationErr_PlainErrorUnchanged(t *testing.T) {
	orig := errors.New("syntax error near (")
	err := wrapMigrationErr("0042_x.sql", orig)
	require.Error(t, err)
	assert.NotContains(t, err.Error(), "sqlite_fts5",
		"non-FTS5 errors must not get the build-tag hint")
	assert.ErrorIs(t, err, orig)
	assert.Contains(t, err.Error(), "0042_x.sql")
}

func TestWrapMigrationErr_NilReturnsNil(t *testing.T) {
	// Defensive: callers may not all check nil before calling. The helper
	// returns nil so `return wrapMigrationErr(...)` is safe even when the
	// underlying call succeeded.
	require.NoError(t, wrapMigrationErr("0001_init.sql", nil))
}

func TestMigrations_RunOnFreshDB(t *testing.T) {
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	defer db.Close()

	require.NoError(t, Migrate(db))

	var version string
	err = db.QueryRow(`SELECT value FROM schema_meta WHERE key='schema_version'`).Scan(&version)
	require.NoError(t, err)
	assert.Equal(t, "4", version)

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

func TestMigrations_0003_AddsRetryColumns(t *testing.T) {
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	require.NoError(t, Migrate(db))

	// schema_version is "4" after 0003 + 0004 (full Migrate runs all).
	// This test's focus is the columns 0003 added; the version check
	// just guards against a regression that drops a later migration.
	var version string
	require.NoError(t, db.QueryRow(
		`SELECT value FROM schema_meta WHERE key='schema_version'`).Scan(&version))
	assert.Equal(t, "4", version)

	// attempts + last_error columns exist on context_embedding.
	// PRAGMA table_info is the canonical way to inspect columns.
	rows, err := db.Query(`PRAGMA table_info(context_embedding)`)
	require.NoError(t, err)
	defer rows.Close()

	cols := map[string]bool{}
	for rows.Next() {
		var cid int
		var name, ctype string
		var notnull, pk int
		var dfltValue any // sqlite gives NULL for columns without default
		require.NoError(t, rows.Scan(&cid, &name, &ctype, &notnull, &dfltValue, &pk))
		cols[name] = true
	}
	assert.True(t, cols["attempts"], "attempts column must exist after 0003")
	assert.True(t, cols["last_error"], "last_error column must exist after 0003")

	// attempts has DEFAULT 0: INSERT without specifying it should succeed
	// and the column should read back as 0.
	_, err = db.Exec(`INSERT INTO context_embedding (item_id, model_slug, embedded_at, status)
		VALUES ('test-0003', 'bge-m3', 0, 'done')`)
	require.NoError(t, err)

	var attempts int
	var lastError sql.NullString
	require.NoError(t, db.QueryRow(
		`SELECT attempts, last_error FROM context_embedding WHERE item_id='test-0003'`).
		Scan(&attempts, &lastError))
	assert.Equal(t, 0, attempts, "attempts must default to 0")
	assert.False(t, lastError.Valid, "last_error must be NULL by default")
}
