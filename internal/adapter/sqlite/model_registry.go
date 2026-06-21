package sqlite

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

// ModelRegistry is the sqlite implementation of port.ModelRegistry. It owns
// the embedding_model table plus the per-slug vec_<slug>_<dim> virtual tables.
// Methods are safe for concurrent use via *sql.DB's connection pool.
type ModelRegistry struct {
	db *sql.DB
}

// NewModelRegistry constructs a registry backed by db. The DB must already
// have run migration 0002 (which seeds the default bge-m3 row).
func NewModelRegistry(db *sql.DB) *ModelRegistry {
	return &ModelRegistry{db: db}
}

// vecTableName derives the physical vec0 table name from a slug + dim.
// Slug dashes are replaced with underscores so the result is a valid SQL
// identifier without quoting. Example: "text-embedding-3-large" @ 3072 →
// "vec_text_embedding_3_large_3072".
func vecTableName(slug string, dim int) string {
	return "vec_" + strings.ReplaceAll(slug, "-", "_") + "_" + strconv.Itoa(dim)
}

// configJSON is the on-disk shape of embedding_model.config. Stored as
// TEXT; parsed back on read.
type configJSON struct {
	BaseURL string `json:"base_url"`
	APIKey  string `json:"api_key"`
}

const selectModelCols = `slug, name, provider, dimension, vec_table, is_default, status, config`

func scanModel(row interface {
	Scan(dest ...any) error
}) (port.ModelDescriptor, error) {
	var (
		m         port.ModelDescriptor
		isDefault int
		cfg       string
	)
	if err := row.Scan(&m.Slug, &m.Name, &m.Provider, &m.Dimension,
		&m.VecTable, &isDefault, &m.Status, &cfg); err != nil {
		return port.ModelDescriptor{}, err
	}
	m.IsDefault = isDefault == 1
	if cfg != "" {
		var c configJSON
		_ = json.Unmarshal([]byte(cfg), &c) // tolerate malformed JSON; surface empty
		m.BaseURL = c.BaseURL
		m.APIKey = c.APIKey
	}
	return m, nil
}

func (r *ModelRegistry) List(ctx context.Context) ([]port.ModelDescriptor, error) {
	rows, err := r.db.QueryContext(ctx,
		`SELECT `+selectModelCols+` FROM embedding_model ORDER BY created_at ASC`)
	if err != nil {
		return nil, fmt.Errorf("list models: %w", err)
	}
	defer rows.Close()

	var out []port.ModelDescriptor
	for rows.Next() {
		m, err := scanModel(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

func (r *ModelRegistry) GetActive(ctx context.Context) (port.ModelDescriptor, error) {
	row := r.db.QueryRowContext(ctx,
		`SELECT `+selectModelCols+` FROM embedding_model WHERE is_default = 1 LIMIT 1`)
	m, err := scanModel(row)
	if err == sql.ErrNoRows {
		return port.ModelDescriptor{}, fmt.Errorf("%w: no default model", domain.ErrNotFound)
	}
	if err != nil {
		return port.ModelDescriptor{}, fmt.Errorf("get active model: %w", err)
	}
	return m, nil
}

func (r *ModelRegistry) Get(ctx context.Context, slug string) (port.ModelDescriptor, error) {
	row := r.db.QueryRowContext(ctx,
		`SELECT `+selectModelCols+` FROM embedding_model WHERE slug = ?`, slug)
	m, err := scanModel(row)
	if err == sql.ErrNoRows {
		return port.ModelDescriptor{}, fmt.Errorf("%w: model %s", domain.ErrNotFound, slug)
	}
	if err != nil {
		return port.ModelDescriptor{}, fmt.Errorf("get model %s: %w", slug, err)
	}
	return m, nil
}

// Register inserts a new model row + creates its vec table in a single
// transaction. Strict insert: errors if slug exists.
func (r *ModelRegistry) Register(ctx context.Context, spec port.ModelSpec) error {
	// Pre-check so we can return a clear error instead of relying on PK
	// violation text (which differs across sqlite versions).
	var existing string
	err := r.db.QueryRowContext(ctx,
		`SELECT slug FROM embedding_model WHERE slug = ?`, spec.Slug).Scan(&existing)
	if err == nil {
		return fmt.Errorf("model %s already registered", spec.Slug)
	}
	if err != sql.ErrNoRows {
		return fmt.Errorf("check existing model %s: %w", spec.Slug, err)
	}

	vecTable := vecTableName(spec.Slug, spec.Dimension)
	cfg, err := json.Marshal(configJSON{BaseURL: spec.BaseURL, APIKey: spec.APIKey})
	if err != nil {
		return fmt.Errorf("encode config: %w", err)
	}

	tx, err := r.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	_, err = tx.ExecContext(ctx, `
		INSERT INTO embedding_model
		    (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
		VALUES (?, ?, ?, ?, ?, 0, 'active', ?, strftime('%s','now'))
	`, spec.Slug, spec.Slug, spec.Provider, spec.Dimension, vecTable, string(cfg))
	if err != nil {
		return fmt.Errorf("insert model row: %w", err)
	}

	createSQL := fmt.Sprintf(`
		CREATE VIRTUAL TABLE IF NOT EXISTS %s USING vec0(
			item_id TEXT PRIMARY KEY,
			embedding FLOAT[%d] distance_metric=cosine
		)
	`, vecTable, spec.Dimension)
	if _, err = tx.ExecContext(ctx, createSQL); err != nil {
		return fmt.Errorf("create vec table %s: %w", vecTable, err)
	}

	if err = tx.Commit(); err != nil {
		return fmt.Errorf("commit register: %w", err)
	}
	return nil
}

// UpdateConfig overwrites provider + config JSON for an existing slug.
// Used to heal Plan 2b alias rows whose config was '{}'.
func (r *ModelRegistry) UpdateConfig(ctx context.Context, slug, baseURL, apiKey, provider string) error {
	cfg, err := json.Marshal(configJSON{BaseURL: baseURL, APIKey: apiKey})
	if err != nil {
		return fmt.Errorf("encode config: %w", err)
	}
	res, err := r.db.ExecContext(ctx, `
		UPDATE embedding_model
		SET provider = ?, config = ?
		WHERE slug = ?
	`, provider, string(cfg), slug)
	if err != nil {
		return fmt.Errorf("update model %s: %w", slug, err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("rows affected: %w", err)
	}
	if n == 0 {
		return fmt.Errorf("%w: model %s", domain.ErrNotFound, slug)
	}
	return nil
}

// SetDefault flips is_default atomically: slug → 1, all others → 0.
func (r *ModelRegistry) SetDefault(ctx context.Context, slug string) error {
	// Pre-check existence so we can return ErrNotFound rather than a silent
	// no-op (the UPDATE would succeed with 0 rows affected otherwise).
	var existing string
	err := r.db.QueryRowContext(ctx,
		`SELECT slug FROM embedding_model WHERE slug = ?`, slug).Scan(&existing)
	if err == sql.ErrNoRows {
		return fmt.Errorf("%w: model %s", domain.ErrNotFound, slug)
	}
	if err != nil {
		return fmt.Errorf("check model %s: %w", slug, err)
	}

	tx, err := r.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	if _, err = tx.ExecContext(ctx,
		`UPDATE embedding_model SET is_default = 0 WHERE slug <> ?`, slug); err != nil {
		return fmt.Errorf("clear old defaults: %w", err)
	}
	if _, err = tx.ExecContext(ctx,
		`UPDATE embedding_model SET is_default = 1 WHERE slug = ?`, slug); err != nil {
		return fmt.Errorf("set new default: %w", err)
	}
	if err = tx.Commit(); err != nil {
		return fmt.Errorf("commit set default: %w", err)
	}
	return nil
}

// Remove drops the vec table + deletes the embedding_model row.
// Refuses default models and shared vec_tables.
func (r *ModelRegistry) Remove(ctx context.Context, slug string) error {
	var (
		isDefault int
		vecTable  string
	)
	err := r.db.QueryRowContext(ctx,
		`SELECT is_default, vec_table FROM embedding_model WHERE slug = ?`, slug).
		Scan(&isDefault, &vecTable)
	if err == sql.ErrNoRows {
		return fmt.Errorf("%w: model %s", domain.ErrNotFound, slug)
	}
	if err != nil {
		return fmt.Errorf("load model %s: %w", slug, err)
	}
	if isDefault == 1 {
		return fmt.Errorf("cannot remove default model %s; switch first", slug)
	}

	// Shared-table protection: Plan 2b alias rows can share a vec_table
	// with the seed. Dropping it would corrupt the other model's vectors.
	var shared int
	qErr := r.db.QueryRowContext(ctx,
		`SELECT count(*) FROM embedding_model WHERE vec_table = ?`, vecTable).Scan(&shared)
	if qErr != nil {
		return fmt.Errorf("check shared vec_table: %w", qErr)
	}
	if shared > 1 {
		return fmt.Errorf("vec table %s shared by %d models; remove dependents first",
			vecTable, shared)
	}

	tx, err := r.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	if _, err = tx.ExecContext(ctx,
		fmt.Sprintf(`DROP TABLE IF EXISTS %s`, vecTable)); err != nil {
		return fmt.Errorf("drop vec table %s: %w", vecTable, err)
	}
	// context_embedding.model_slug FK is RESTRICT (no ON DELETE clause in
	// migration 0002). This explicit DELETE is mandatory; without it, the
	// row delete below would raise a FK constraint violation.
	if _, err = tx.ExecContext(ctx,
		`DELETE FROM context_embedding WHERE model_slug = ?`, slug); err != nil {
		return fmt.Errorf("delete status rows for model %s: %w", slug, err)
	}
	if _, err = tx.ExecContext(ctx,
		`DELETE FROM embedding_model WHERE slug = ?`, slug); err != nil {
		return fmt.Errorf("delete model %s: %w", slug, err)
	}

	if err = tx.Commit(); err != nil {
		return fmt.Errorf("commit remove: %w", err)
	}
	return nil
}
