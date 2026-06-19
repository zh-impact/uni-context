package sqlite

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"
	"regexp"
	"sort"
	"strconv"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

var versionRE = regexp.MustCompile(`(\d+)_.*\.sql$`)

// Migrate runs all pending migrations in order.
func Migrate(db *sql.DB) error {
	if err := ensureSchemaMeta(db); err != nil {
		return err
	}
	current, err := readVersion(db)
	if err != nil {
		return err
	}

	files, err := sortedMigrationFiles()
	if err != nil {
		return err
	}

	for _, fname := range files {
		v := versionFromName(fname)
		if v <= current {
			continue
		}
		content, err := migrationFS.ReadFile("migrations/" + fname)
		if err != nil {
			return fmt.Errorf("read migration %s: %w", fname, err)
		}
		if err := execMigration(db, fname, string(content)); err != nil {
			return err
		}
	}
	return nil
}

func ensureSchemaMeta(db *sql.DB) error {
	_, err := db.Exec(`CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )`)
	if err != nil {
		return err
	}
	_, err = db.Exec(`INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '0')`)
	return err
}

func readVersion(db *sql.DB) (int, error) {
	var s string
	err := db.QueryRow(`SELECT value FROM schema_meta WHERE key='schema_version'`).Scan(&s)
	if err != nil {
		return 0, err
	}
	return strconv.Atoi(s)
}

func sortedMigrationFiles() ([]string, error) {
	entries, err := fs.ReadDir(migrationFS, "migrations")
	if err != nil {
		return nil, err
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	return names, nil
}

func versionFromName(name string) int {
	m := versionRE.FindStringSubmatch(name)
	if len(m) < 2 {
		return 0
	}
	v, _ := strconv.Atoi(m[1])
	return v
}

// execMigration wraps the entire migration body in a single transaction
// (SQLite handles DDL transactionally). It does NOT parse statements —
// migrations are authored to be executable as one Exec call.
func execMigration(db *sql.DB, fname, body string) error {
	tx, err := db.BeginTx(context.Background(), nil)
	if err != nil {
		return fmt.Errorf("begin tx for %s: %w", fname, err)
	}
	if _, err := tx.Exec(body); err != nil {
		_ = tx.Rollback()
		return fmt.Errorf("exec migration %s: %w", fname, err)
	}
	return tx.Commit()
}
