package sqlite

import (
	"database/sql"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	_ "github.com/mattn/go-sqlite3"
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
