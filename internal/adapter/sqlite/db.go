package sqlite

import (
	"database/sql"
	"fmt"

	sqlite_vec "github.com/asg017/sqlite-vec-go-bindings/cgo"
	_ "github.com/mattn/go-sqlite3"
)

// init registers the vec0 module process-globally before any sql.Open
// call. sqlite-vec-go-bindings' Auto() hooks mattn/go-sqlite3's driver
// so every "sqlite3" connection in this process supports vec0 virtual
// tables. Idempotent.
func init() {
	sqlite_vec.Auto()
}

// Open opens a SQLite database at dbPath (file path or ":memory:") with the
// PRAGMAs specified in the global constraints, then runs migrations.
//
// Note on WAL + :memory:: SQLite silently ignores `_journal_mode=WAL`
// for in-memory databases — they always use MEMORY journal. This is
// cosmetic (in-memory DBs have no cross-process readers to benefit from
// WAL anyway), but worth knowing if you're debugging test behavior:
// tests that pass `:memory:` won't exercise WAL. File-based tests do.
func Open(dbPath string) (*sql.DB, error) {
	dsn := fmt.Sprintf("file:%s?_journal_mode=WAL&_synchronous=NORMAL&_busy_timeout=5000&_foreign_keys=on&_temp_store=MEMORY", dbPath)
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	// SQLite doesn't error on open until first use; ping to surface issues.
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}
	if err := Migrate(db); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("migrate: %w", err)
	}
	return db, nil
}
