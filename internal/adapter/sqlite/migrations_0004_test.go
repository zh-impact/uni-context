package sqlite

import (
	"database/sql"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// openMigratedDBFor0004 returns a fresh in-memory DB with all migrations
// applied and FK enforcement ON. FKs are OFF by default in SQLite; the
// cascade assertions below are silent without this DSN.
func openMigratedDBFor0004(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", "file::memory:?_foreign_keys=on")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })
	require.NoError(t, Migrate(db))
	return db
}

// TestMigration0004_CascadesOnEmbeddingModelDelete verifies that after the
// migration, deleting an embedding_model row automatically drops all its
// context_embedding status rows. Migration 0002 declared the FK without
// an ON DELETE clause (default RESTRICT); 0004 rebuilds the table with
// ON DELETE CASCADE so ModelRegistry.Remove's explicit DELETE becomes
// defense-in-depth rather than mandatory.
func TestMigration0004_CascadesOnEmbeddingModelDelete(t *testing.T) {
	db := openMigratedDBFor0004(t)

	// Seed a non-default model + item + status row.
	_, err := db.Exec(`
		INSERT INTO context_item (id, scope, kind, source, owner_user_id, title, content, created_at, updated_at)
		VALUES ('item-1', 'user', 'note', 'test', 'u', 't', 'c', 0, 0);
		INSERT INTO embedding_model (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
		VALUES ('test-slug', 'test', 'ollama', 8, 'vec_test_slug_8', 0, 'active', '{}', 0);
		INSERT INTO context_embedding (item_id, model_slug, embedded_at, status, attempts)
		VALUES ('item-1', 'test-slug', 0, 'done', 1);
	`)
	require.NoError(t, err)

	_, err = db.Exec(`DELETE FROM embedding_model WHERE slug = 'test-slug'`)
	require.NoError(t, err, "DELETE should succeed; CASCADE should drop status rows")

	var n int
	require.NoError(t, db.QueryRow(
		`SELECT count(*) FROM context_embedding WHERE model_slug = 'test-slug'`).Scan(&n))
	assert.Equal(t, 0, n, "FK CASCADE must drop context_embedding rows")
}

// TestMigration0004_PreservesContextItemCascade locks in that the existing
// item_id -> context_item(id) ON DELETE CASCADE (migration 0002) still
// fires after the rebuild. Regression guard: a careless rebuild could
// drop the existing CASCADE clause.
func TestMigration0004_PreservesContextItemCascade(t *testing.T) {
	db := openMigratedDBFor0004(t)

	_, err := db.Exec(`
		INSERT INTO context_item (id, scope, kind, source, owner_user_id, title, content, created_at, updated_at)
		VALUES ('item-2', 'user', 'note', 'test', 'u', 't', 'c', 0, 0);
		INSERT INTO context_embedding (item_id, model_slug, embedded_at, status, attempts)
		VALUES ('item-2', 'bge-m3', 0, 'done', 1);
	`)
	require.NoError(t, err)

	_, err = db.Exec(`DELETE FROM context_item WHERE id = 'item-2'`)
	require.NoError(t, err)

	var n int
	require.NoError(t, db.QueryRow(
		`SELECT count(*) FROM context_embedding WHERE item_id = 'item-2'`).Scan(&n))
	assert.Equal(t, 0, n, "context_item delete must still cascade")
}

// TestMigration0004_DataPreservedAcrossRebuild confirms the INSERT INTO
// ... SELECT copy step carries every column (including 0003's attempts +
// last_error) without dropping data.
func TestMigration0004_DataPreservedAcrossRebuild(t *testing.T) {
	db := openMigratedDBFor0004(t)

	_, err := db.Exec(`
		INSERT INTO context_item (id, scope, kind, source, owner_user_id, title, content, created_at, updated_at)
		VALUES ('item-3', 'user', 'note', 'test', 'u', 't', 'c', 0, 0);
		INSERT INTO context_embedding (item_id, model_slug, embedded_at, status, error, attempts, last_error)
		VALUES ('item-3', 'bge-m3', 42, 'failed', 'orig err', 7, 'latest err');
	`)
	require.NoError(t, err)

	var (
		embAt    int
		status   string
		errMsg   sql.NullString
		attempts int
		lastErr  sql.NullString
	)
	require.NoError(t, db.QueryRow(`
		SELECT embedded_at, status, error, attempts, last_error
		FROM context_embedding WHERE item_id = 'item-3'`).
		Scan(&embAt, &status, &errMsg, &attempts, &lastErr))
	assert.Equal(t, 42, embAt)
	assert.Equal(t, "failed", status)
	assert.True(t, errMsg.Valid)
	assert.Equal(t, "orig err", errMsg.String)
	assert.Equal(t, 7, attempts)
	assert.True(t, lastErr.Valid)
	assert.Equal(t, "latest err", lastErr.String)
}
