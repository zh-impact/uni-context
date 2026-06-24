package sqlite

import (
	"context"
	"database/sql"
	"fmt"
)

// SchemaMetaRepo reads the schema_meta table. Implements port.SchemaMeta
// so DiagnosticService can surface the migration version without the CLI
// reaching into *sql.DB. The query mirrors the unexported readVersion in
// migrations.go (line 63) — kept separate so the doctor path can evolve
// independently of the migration runner.
type SchemaMetaRepo struct {
	db *sql.DB
}

func NewSchemaMetaRepo(db *sql.DB) *SchemaMetaRepo {
	return &SchemaMetaRepo{db: db}
}

func (r *SchemaMetaRepo) Version(ctx context.Context) (string, error) {
	var v string
	err := r.db.QueryRowContext(ctx,
		`SELECT value FROM schema_meta WHERE key='schema_version'`).Scan(&v)
	if err != nil {
		return "", fmt.Errorf("read schema_version: %w", err)
	}
	return v, nil
}
