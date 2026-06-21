package sqlite

import (
	"database/sql"
	"fmt"
	"os"
	"strings"

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
// For file-backed DBs, the file mode is tightened to 0600 after Migrate
// succeeds: Plan 2c persists API keys inside embedding_model.config JSON,
// and a group/world-readable DB would leak them on shared systems. The
// tightening is best-effort — stat/chmod failures are logged to stderr
// but never fatal. :memory: DSNs skip chmod entirely.
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
	tightenDBFilePermissions(dbPath)
	return db, nil
}

// tightenDBFilePermissions tightens the on-disk DB file mode to 0600 if it
// currently has any group/other bits set. Emits a one-time stderr warning
// when it actually changes the mode. All errors are non-fatal: API-key
// safety is best-effort and must not break Open. No-op for ":memory:".
//
// dbPath is the caller-facing path (the same value passed to Open), not the
// mattn/go-sqlite3 DSN. The DSN is built inside Open as `file:<path>?...`,
// so we treat dbPath directly as the filesystem location. A "file:" prefix
// or "?query" suffix is stripped defensively in case a future caller hands
// us a DSN-shaped string.
func tightenDBFilePermissions(dbPath string) {
	if dbPath == "" || dbPath == ":memory:" {
		return
	}
	path := dbPath
	if strings.HasPrefix(path, "file:") {
		path = strings.TrimPrefix(path, "file:")
	}
	if i := strings.IndexByte(path, '?'); i >= 0 {
		path = path[:i]
	}
	if path == "" || path == ":memory:" {
		return
	}

	info, err := os.Stat(path)
	if err != nil {
		fmt.Fprintf(os.Stderr, "warning: stat %s for perm check failed: %v\n", path, err)
		return
	}
	// Group/other bits set? -> tighten.
	if info.Mode().Perm()&0o077 == 0 {
		return
	}
	if err := os.Chmod(path, 0o600); err != nil {
		fmt.Fprintf(os.Stderr, "warning: chmod %s to 0600 failed: %v\n", path, err)
		return
	}
	fmt.Fprintf(os.Stderr, "warning: tightened %s permissions to 0600 (contains API keys)\n", path)
}
