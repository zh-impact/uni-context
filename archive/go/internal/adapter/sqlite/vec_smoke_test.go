package sqlite

import (
	"context"
	"database/sql"
	"testing"

	sqlite_vec "github.com/asg017/sqlite-vec-go-bindings/cgo"
	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/require"
)

// TestVec0_SmokeTest proves the sqlite-vec cgo extension is wired up:
// we can CREATE a vec0 virtual table, insert a serialized vector, and
// run a KNN query. This test is the canary for "did Auto() get called
// before sql.Open?" — if not, CREATE fails with "no such module: vec0".
func TestVec0_SmokeTest(t *testing.T) {
	// Auto() is process-global and idempotent; safe to call here.
	sqlite_vec.Auto()

	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	_, err = db.Exec(`CREATE VIRTUAL TABLE test_vec USING vec0(embedding float[4])`)
	require.NoError(t, err, "vec0 module must be registered via sqlite_vec.Auto()")

	v1, err := sqlite_vec.SerializeFloat32([]float32{1.0, 0.0, 0.0, 0.0})
	require.NoError(t, err)
	v2, err := sqlite_vec.SerializeFloat32([]float32{0.0, 1.0, 0.0, 0.0})
	require.NoError(t, err)
	_, err = db.Exec(`INSERT INTO test_vec(rowid, embedding) VALUES (1, ?), (2, ?)`, v1, v2)
	require.NoError(t, err)

	q, err := sqlite_vec.SerializeFloat32([]float32{1.0, 0.1, 0.0, 0.0})
	require.NoError(t, err)
	rows, err := db.QueryContext(context.Background(),
		`SELECT rowid, distance FROM test_vec WHERE embedding MATCH ? ORDER BY distance LIMIT 1`, q)
	require.NoError(t, err)
	defer rows.Close()

	require.True(t, rows.Next())
	var rowid int64
	var dist float64
	require.NoError(t, rows.Scan(&rowid, &dist))
	require.EqualValues(t, 1, rowid, "closest vector to query should be rowid 1")
}
