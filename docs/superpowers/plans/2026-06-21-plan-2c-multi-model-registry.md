# Plan 2c — Multi-Model Registry & Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `EnsureModelRegistered` placeholder with a real runtime model registry, add CLI commands for model lifecycle (`add`/`list`/`remove`/`switch`/`reembed`), and migrate the active-model source-of-truth from `config.Embedder.Model` to `embedding_model.is_default` — with a one-time reconciliation gated by `schema_meta`.

**Architecture:** A single `port.Embedder` is constructed at `app.Wire` time from whichever `embedding_model` row has `is_default=1`. The registry (`port.ModelRegistry` + sqlite impl) owns add/list/remove + DDL for per-slug `vec_<slug>_<dim>` tables. `embed switch` is a transactional metadata flip; `embed reembed` is a separate bulk-work command reusing Plan 2b's status-row mechanism. No schema migration.

**Tech Stack:** Go 1.25, cgo + mattn/go-sqlite3, sqlite-vec (vec0 virtual tables via `sqlite_vec.Auto()` registered process-globally in `internal/adapter/sqlite/db.go`), cobra CLI, testify.

## Global Constraints

Copied verbatim from `docs/superpowers/specs/2026-06-21-plan-2c-multi-model-registry-design.md`:

- **No new SQL migration file.** Existing `embedding_model` + `context_embedding` + vec0 tables from migrations 0002/0003 are multi-model-capable.
- **`port.Embedder` interface unchanged** — single-model semantics preserved.
- **`EmbedService.Embed(ctx, itemID, title, content)` signature unchanged** — embeds into `s.embedder.Model().Slug`.
- **`SearchService`, `BackfillService`, `WorkerService`, `EmbeddingRepo` unchanged** (worker is already model-agnostic via `status='failed'` rows).
- **`EnsureModelRegistered` removed** (in `internal/adapter/sqlite/model_registry.go`) — replaced by `ModelRegistry.Register`.
- **`vec_table` column is the source of truth** for which physical table holds a model's vectors. Plan 2c does not rename existing rows.
- **Plan 2b alias rows protected on `Remove`**: if `SELECT COUNT(*) FROM embedding_model WHERE vec_table = ?` > 1, refuse.
- **First-Plan-2c-run reconciliation gated by `schema_meta.plan_2c_synced`** — after first Wire, DB is authoritative; `config.Embedder` (except `enabled`) is ignored.
- **Per-slug vec table naming**: `"vec_" + strings.ReplaceAll(slug, "-", "_") + "_" + strconv.Itoa(dim)`. Example: `text-embedding-3-large` @ 3072 → `vec_text_embedding_3_large_3072`.
- **API keys persist in `embedding_model.config` JSON column** as `{"base_url":"...","api_key":"..."}`. CHANGELOG must warn about DB file perms.
- **`domain.ErrNotFound`** sentinel for missing rows; wrapping pattern: `fmt.Errorf("%w: model %s", domain.ErrNotFound, slug)`. Test assertion: `assert.ErrorIs(t, err, domain.ErrNotFound)`.
- **Build/test invocation**: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./...` (or per-package path). Tests use testify (`require`/`assert`).
- **Format**: `goimports -w` on every touched `.go` file before commit.
- **Commit messages**: conventional commits (`feat(...)`, `fix(...)`, `docs(...)`, `refactor(...)`, `test(...)`).
- **Schema_meta convention**: `INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('<key>', '<value>')`. Already used by migrations; safe to call from app code.

---

## File Structure

**New files:**
- `internal/port/modelregistry.go` — `ModelDescriptor`, `ModelSpec`, `ModelRegistry` interface.
- `internal/service/reembed.go` — `ReembedService` + `ReembedReport` + `ReembedFailure`.
- `internal/service/reembed_test.go` — unit tests.

**Rewritten files:**
- `internal/adapter/sqlite/model_registry.go` — `EnsureModelRegistered` removed; full `ModelRegistry` impl added.
- `internal/adapter/sqlite/model_registry_test.go` — old `TestEnsureModelRegistered_*` tests removed; `TestModelRegistry_*` tests added.

**Modified files:**
- `internal/app/app.go` — `reconcilePlan2cSync` added; `EnsureModelRegistered` call replaced by registry wiring; embedder constructed from active model's provider/base_url/api_key.
- `internal/cli/embed.go` — new `embedModelCmd` parent + `embedModelAddCmd`/`embedModelListCmd`/`embedModelRemoveCmd`; new `embedSwitchCmd`; new `embedReembedCmd`. Existing `embedBackfillCmd`/`embedWorkerCmd` unchanged.
- `internal/cli/embed_test.go` — extend structural test to count 5 subcommands under `embed` (backfill, worker, reembed, switch, model).
- `CHANGELOG.md` — new Plan 2c section.

**Unchanged (do NOT modify):**
- `internal/port/embedder.go`
- `internal/port/embeddingrepo.go`
- `internal/service/embed.go`, `search.go`, `backfill.go`, `worker.go`
- `internal/adapter/sqlite/embedding_repo.go`, `vectorstore.go`, `searcher.go`, `repo.go`, `project_repo.go`, `db.go`, `migrations.go`
- `internal/adapter/sqlite/migrations/0001_init.sql`, `0002_embeddings.sql`, `0003_embedding_retry.sql`
- `internal/adapter/embedder/ollama/ollama.go`, `internal/adapter/embedder/openai/openai.go`
- `internal/adapter/fsstore/*.go`
- `internal/config/config.go`
- `internal/domain/*.go`

---

## Task 1: ModelRegistry port + sqlite implementation

**Goal:** Replace the `EnsureModelRegistered` placeholder with a real `ModelRegistry`. Implements add/get/list/default/update/remove with transactional DDL for new vec tables and shared-table protection on remove.

**Files:**
- Create: `internal/port/modelregistry.go`
- Rewrite: `internal/adapter/sqlite/model_registry.go` (delete `EnsureModelRegistered`; add `ModelRegistry` impl)
- Rewrite: `internal/adapter/sqlite/model_registry_test.go` (delete `TestEnsureModelRegistered_*`; add `TestModelRegistry_*`)

**Interfaces:**
- Consumes: `domain.ErrNotFound` (existing), `sqlite_vec` via `CREATE VIRTUAL TABLE ... USING vec0(...)` DDL, existing `embedding_model` schema (migration 0002)
- Produces:
  - `port.ModelDescriptor{Slug, Name, Provider, BaseURL, APIKey string; Dimension int; VecTable string; IsDefault bool; Status string}`
  - `port.ModelSpec{Slug, Provider, BaseURL, APIKey string; Dimension int}`
  - `port.ModelRegistry` interface (see below)
  - `sqlite.NewModelRegistry(db *sql.DB) *ModelRegistry`
  - Removes: `sqlite.EnsureModelRegistered` (callers in `internal/app/app.go` updated in Task 3)

- [ ] **Step 1: Write `internal/port/modelregistry.go`**

```go
package port

import "context"

// ModelDescriptor is the full projection of an embedding_model row.
// BaseURL and APIKey come from the row's config JSON column
// ({"base_url":"...","api_key":"..."}).
type ModelDescriptor struct {
	Slug      string
	Name      string
	Provider  string
	BaseURL   string
	APIKey    string
	Dimension int
	VecTable  string
	IsDefault bool
	Status    string // "active" | "disabled"
}

// ModelSpec is the input to ModelRegistry.Register.
type ModelSpec struct {
	Slug      string
	Provider  string
	BaseURL   string
	APIKey    string
	Dimension int
}

// ModelRegistry owns the embedding_model table. Methods are concurrency-safe
// via the underlying *sql.DB's connection pool; callers do not need external
// locking.
type ModelRegistry interface {
	// List returns all registered models ordered by created_at ASC.
	List(ctx context.Context) ([]ModelDescriptor, error)

	// GetActive returns the row with is_default=1. Returns a wrapping of
	// domain.ErrNotFound if no row is default.
	GetActive(ctx context.Context) (ModelDescriptor, error)

	// Get returns the row for slug. Returns a wrapping of domain.ErrNotFound
	// if slug is not registered.
	Get(ctx context.Context, slug string) (ModelDescriptor, error)

	// Register inserts a new model row and creates its vec_<slug>_<dim>
	// virtual table. Strict INSERT: returns an error if slug already exists.
	// Callers needing upsert behavior (e.g. reconcilePlan2cSync) must
	// explicitly check Get first and call UpdateConfig.
	Register(ctx context.Context, spec ModelSpec) error

	// UpdateConfig overwrites provider + config JSON for an existing slug.
	// Used by the first-Plan-2c-run reconciliation to heal Plan 2b alias
	// rows whose config column was '{}'. Returns a wrapping of
	// domain.ErrNotFound if slug does not exist.
	UpdateConfig(ctx context.Context, slug, baseURL, apiKey, provider string) error

	// SetDefault flips is_default atomically: slug gets 1, all others get 0.
	// Idempotent if slug is already default. Returns a wrapping of
	// domain.ErrNotFound if slug does not exist.
	SetDefault(ctx context.Context, slug string) error

	// Remove drops the model's vec table and deletes its embedding_model row.
	// context_embedding rows cascade-delete via FK ON DELETE CASCADE.
	// Refuses with a clear error if:
	//   - slug does not exist (wraps domain.ErrNotFound)
	//   - slug is_default=1 (caller must switch first)
	//   - vec_table is referenced by another slug (shared-table protection)
	Remove(ctx context.Context, slug string) error
}
```

- [ ] **Step 2: Write the test file `internal/adapter/sqlite/model_registry_test.go` (rewrite)**

Replace the entire file contents. The old `EnsureModelRegistered` tests are deleted; the helper `openTestDB` is preserved.

```go
package sqlite

import (
	"context"
	"database/sql"
	"testing"

	"uni-context/internal/domain"
	"uni-context/internal/port"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// openTestDB gives each test a fresh migrated in-memory DB. The 0002
// migration seeds slug='bge-m3', dimension=1024, vec_table='vec_bge_m3_1024',
// is_default=1.
func openTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })
	require.NoError(t, Migrate(db))
	return db
}

func TestModelRegistry_GetActive_ReturnsSeedDefault(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)

	active, err := reg.GetActive(context.Background())
	require.NoError(t, err)
	assert.Equal(t, "bge-m3", active.Slug)
	assert.True(t, active.IsDefault)
	assert.Equal(t, "vec_bge_m3_1024", active.VecTable)
	assert.Equal(t, 1024, active.Dimension)
	// 0002 seed config JSON has base_url; api_key empty.
	assert.Equal(t, "http://localhost:11434", active.BaseURL)
	assert.Empty(t, active.APIKey)
}

func TestModelRegistry_Get_MissingSlugIsNotFound(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)

	_, err := reg.Get(context.Background(), "nonexistent")
	require.Error(t, err)
	assert.ErrorIs(t, err, domain.ErrNotFound)
}

func TestModelRegistry_Register_CreatesRowAndVecTable(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)
	ctx := context.Background()

	err := reg.Register(ctx, port.ModelSpec{
		Slug: "text-embedding-3-large", Provider: "openai",
		BaseURL: "https://api.openai.com/v1", APIKey: "sk-test",
		Dimension: 3072,
	})
	require.NoError(t, err)

	// Row exists with right fields.
	got, err := reg.Get(ctx, "text-embedding-3-large")
	require.NoError(t, err)
	assert.Equal(t, "text-embedding-3-large", got.Slug)
	assert.Equal(t, "openai", got.Provider)
	assert.Equal(t, "https://api.openai.com/v1", got.BaseURL)
	assert.Equal(t, "sk-test", got.APIKey)
	assert.Equal(t, 3072, got.Dimension)
	assert.Equal(t, "vec_text_embedding_3_large_3072", got.VecTable)
	assert.False(t, got.IsDefault, "new model is not default")

	// Vec table exists and accepts inserts at the right dimension.
	var n int
	require.NoError(t, db.QueryRow(
		`SELECT count(*) FROM sqlite_master WHERE type='table' AND name='vec_text_embedding_3_large_3072'`).Scan(&n))
	assert.Equal(t, 1, n, "vec table created")
}

func TestModelRegistry_Register_RejectsExistingSlug(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)
	ctx := context.Background()

	err := reg.Register(ctx, port.ModelSpec{
		Slug: "bge-m3", Provider: "ollama", Dimension: 1024,
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "already registered")
}

func TestModelRegistry_SetDefault_AtomicFlip(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)
	ctx := context.Background()

	require.NoError(t, reg.Register(ctx, port.ModelSpec{
		Slug: "nomic-embed-text", Provider: "openai", Dimension: 768,
	}))
	require.NoError(t, reg.SetDefault(ctx, "nomic-embed-text"))

	// Exactly one is_default=1 row, and it's the new slug.
	var n, activeSlug int
	require.NoError(t, db.QueryRow(
		`SELECT count(*) FROM embedding_model WHERE is_default=1`).Scan(&n))
	assert.Equal(t, 1, n, "exactly one default")
	require.NoError(t, db.QueryRow(
		`SELECT count(*) FROM embedding_model WHERE slug='nomic-embed-text' AND is_default=1`).Scan(&activeSlug))
	assert.Equal(t, 1, activeSlug)
}

func TestModelRegistry_SetDefault_Idempotent(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)

	// bge-m3 is already default from 0002 seed.
	require.NoError(t, reg.SetDefault(context.Background(), "bge-m3"))

	active, err := reg.GetActive(context.Background())
	require.NoError(t, err)
	assert.Equal(t, "bge-m3", active.Slug)
}

func TestModelRegistry_SetDefault_UnknownSlugIsNotFound(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)

	err := reg.SetDefault(context.Background(), "ghost")
	require.Error(t, err)
	assert.ErrorIs(t, err, domain.ErrNotFound)
}

func TestModelRegistry_UpdateConfig_HealsExistingRow(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)
	ctx := context.Background()

	// Simulate a Plan 2b alias row: existing slug, config='{}'.
	// Insert one directly to control starting state.
	_, err := db.Exec(`
		INSERT INTO embedding_model
		    (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
		VALUES ('alias-slug', 'alias-slug', 'ollama', 1024, 'vec_bge_m3_1024', 0, 'active', '{}', strftime('%s','now'))`)
	require.NoError(t, err)

	require.NoError(t, reg.UpdateConfig(ctx, "alias-slug",
		"http://lmstudio:1234/v1", "sk-lm", "openai"))

	got, err := reg.Get(ctx, "alias-slug")
	require.NoError(t, err)
	assert.Equal(t, "openai", got.Provider)
	assert.Equal(t, "http://lmstudio:1234/v1", got.BaseURL)
	assert.Equal(t, "sk-lm", got.APIKey)
	assert.Equal(t, "vec_bge_m3_1024", got.VecTable, "vec_table untouched")
}

func TestModelRegistry_UpdateConfig_UnknownSlugIsNotFound(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)

	err := reg.UpdateConfig(context.Background(), "ghost", "u", "k", "openai")
	require.Error(t, err)
	assert.ErrorIs(t, err, domain.ErrNotFound)
}

func TestModelRegistry_Remove_RejectsDefault(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)

	err := reg.Remove(context.Background(), "bge-m3") // is_default=1
	require.Error(t, err)
	assert.Contains(t, err.Error(), "default")
}

func TestModelRegistry_Remove_RejectsSharedVecTable(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)

	// Manually insert a second row pointing at the seeded vec table
	// (simulates Plan 2b alias registration).
	_, err := db.Exec(`
		INSERT INTO embedding_model
		    (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
		VALUES ('bge-m3-alias', 'bge-m3-alias', 'ollama', 1024, 'vec_bge_m3_1024', 0, 'active', '{}', strftime('%s','now'))`)
	require.NoError(t, err)

	err = reg.Remove(context.Background(), "bge-m3-alias")
	require.Error(t, err)
	assert.Contains(t, err.Error(), "shared")
}

func TestModelRegistry_Remove_NonDefaultSucceeds(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)
	ctx := context.Background()

	// Register a fresh model with its own vec table, then remove it.
	require.NoError(t, reg.Register(ctx, port.ModelSpec{
		Slug: "nomic-embed-text", Provider: "openai", Dimension: 768,
	}))
	require.NoError(t, reg.Remove(ctx, "nomic-embed-text"))

	// Row gone.
	_, err := reg.Get(ctx, "nomic-embed-text")
	require.Error(t, err)
	assert.ErrorIs(t, err, domain.ErrNotFound)

	// Vec table gone.
	var n int
	require.NoError(t, db.QueryRow(
		`SELECT count(*) FROM sqlite_master WHERE type='table' AND name='vec_nomic_embed_text_768'`).Scan(&n))
	assert.Equal(t, 0, n, "vec table dropped")
}

func TestModelRegistry_Remove_CascadesEmbeddingStatusRows(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)
	ctx := context.Background()

	require.NoError(t, reg.Register(ctx, port.ModelSpec{
		Slug: "nomic-embed-text", Provider: "openai", Dimension: 768,
	}))

	// Insert a fake context_item + context_embedding row referencing the model.
	_, err := db.Exec(`
		INSERT INTO context_item (id, user_id, scope, kind, title, content, created_at, updated_at)
		VALUES ('item-1', 'default', 'user', 'note', 't', 'c', 0, 0)`)
	require.NoError(t, err)
	_, err = db.Exec(`
		INSERT INTO context_embedding (item_id, model_slug, embedded_at, status, attempts)
		VALUES ('item-1', 'nomic-embed-text', 0, 'done', 1)`)
	require.NoError(t, err)

	require.NoError(t, reg.Remove(ctx, "nomic-embed-text"))

	// context_embedding row should be cascade-deleted.
	var n int
	require.NoError(t, db.QueryRow(
		`SELECT count(*) FROM context_embedding WHERE model_slug='nomic-embed-text'`).Scan(&n))
	assert.Equal(t, 0, n, "status rows cascade-deleted")
}

func TestModelRegistry_List_OrdersByCreation(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)
	ctx := context.Background()

	require.NoError(t, reg.Register(ctx, port.ModelSpec{
		Slug: "second", Provider: "openai", Dimension: 768,
	}))
	require.NoError(t, reg.Register(ctx, port.ModelSpec{
		Slug: "third", Provider: "openai", Dimension: 1536,
	}))

	all, err := reg.List(ctx)
	require.NoError(t, err)
	require.Len(t, all, 3) // bge-m3 seed + second + third
	assert.Equal(t, "bge-m3", all[0].Slug, "seed first")
	assert.Equal(t, "second", all[1].Slug)
	assert.Equal(t, "third", all[2].Slug)
}
```

- [ ] **Step 3: Run tests to verify RED**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestModelRegistry -v ./internal/adapter/sqlite/...`

Expected: compile error (no `NewModelRegistry`, no `ModelRegistry` methods). Each `TestModelRegistry_*` fails to compile.

- [ ] **Step 4: Implement `internal/adapter/sqlite/model_registry.go` (full rewrite)**

Replace the entire file. Delete `EnsureModelRegistered`; add the `ModelRegistry` type + all methods + the `vecTableName` helper.

```go
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
	require := r.db.QueryRowContext(ctx,
		`SELECT count(*) FROM embedding_model WHERE vec_table = ?`, vecTable).Scan(&shared)
	if require != nil {
		return fmt.Errorf("check shared vec_table: %w", require)
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
	if _, err = tx.ExecContext(ctx,
		`DELETE FROM embedding_model WHERE slug = ?`, slug); err != nil {
		return fmt.Errorf("delete model %s: %w", slug, err)
	}
	// context_embedding rows cascade-delete via FK ON DELETE CASCADE
	// declared in migration 0002.

	if err = tx.Commit(); err != nil {
		return fmt.Errorf("commit remove: %w", err)
	}
	return nil
}
```

- [ ] **Step 5: Run tests to verify GREEN**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestModelRegistry -v ./internal/adapter/sqlite/...`

Expected: all `TestModelRegistry_*` tests PASS.

- [ ] **Step 6: Build check (catches the `EnsureModelRegistered` removal)**

Run: `CGO_ENABLED=1 go build -tags sqlite_fts5 ./...`

Expected: build failure in `internal/app/app.go` referencing the deleted `EnsureModelRegistered`. This is expected; Task 3 fixes the caller. **Do not fix it here.** Confirm the failure mentions `EnsureModelRegistered` and proceed.

- [ ] **Step 7: goimports + commit**

```bash
goimports -w internal/port/modelregistry.go internal/adapter/sqlite/model_registry.go internal/adapter/sqlite/model_registry_test.go
git add internal/port/modelregistry.go internal/adapter/sqlite/model_registry.go internal/adapter/sqlite/model_registry_test.go
git commit -m "$(cat <<'EOF'
feat(sqlite,port): add ModelRegistry; remove EnsureModelRegistered

Replaces the Plan 2a EnsureModelRegistered placeholder with a real
ModelRegistry port + sqlite implementation. Supports add/get/list/
default/update/remove with transactional DDL for per-slug vec tables
and shared-table protection that preserves Plan 2b alias rows.

app.Wire still references the deleted function; Task 3 fixes the caller.
EOF
)"
```

---

## Task 2: ReembedService

**Goal:** Add `ReembedService` for bulk re-embedding items under the active model. Mirrors `BackfillService`'s shape but filters on "no status='done' row for the active model" instead of "any_embedding=0".

**Files:**
- Create: `internal/service/reembed.go`
- Create: `internal/service/reembed_test.go`

**Interfaces:**
- Consumes:
  - `port.ContextRepo` with `List(ctx, port.ItemFilter) ([]domain.ContextItem, string, error)`
  - `*EmbedService` with `Embed(ctx, itemID, title, content string) error`
  - `port.ModelInfo{Slug string; Dimension int}` (the active model — passed at construction time)
- Produces:
  - `service.NewReembedService(repo port.ContextRepo, embed *EmbedService, active port.ModelInfo) *ReembedService`
  - `(*ReembedService).Run(ctx context.Context, limit int, dryRun bool) (ReembedReport, error)`
  - `service.ReembedReport{Scanned, Embedded, Failed int; Failures []ReembedFailure}`
  - `service.ReembedFailure{ItemID, Error string}`

- [ ] **Step 1: Write the test file `internal/service/reembed_test.go`**

```go
package service

import (
	"context"
	"errors"
	"testing"

	"uni-context/internal/domain"
	"uni-context/internal/port"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// fakeListRepo is a minimal ContextRepo stub for ReembedService tests.
// Only List is exercised; other methods panic if called unexpectedly.
type fakeListRepo struct {
	items []domain.ContextItem
}

func (f *fakeListRepo) Create(ctx context.Context, item domain.ContextItem) error {
	panic("unexpected")
}
func (f *fakeListRepo) Update(ctx context.Context, item domain.ContextItem) error {
	panic("unexpected")
}
func (f *fakeListRepo) Delete(ctx context.Context, id string) error {
	panic("unexpected")
}
func (f *fakeListRepo) Get(ctx context.Context, id string) (domain.ContextItem, error) {
	panic("unexpected")
}
func (f *fakeListRepo) List(ctx context.Context, f2 port.ItemFilter) ([]domain.ContextItem, string, error) {
	return f.items, "", nil
}

// embedSpy captures Embed calls so tests can assert behavior without a
// real EmbedService. We can't substitute *EmbedService directly (concrete
// type), so tests construct a real EmbedService with a fake embedder and
// assert side effects via the embeddingRepo. For unit-test simplicity
// here, we instead inject the embed-call function via a thin wrapper.
//
// To keep the production ReembedService signature using *EmbedService,
// tests build a real EmbedService with port.Embedder = embedSpy.
type embedSpy struct {
	calls     []string
	errOn     map[string]error // itemID → error to return
}

func (e *embedSpy) Model() port.ModelInfo {
	return port.ModelInfo{Slug: "active-model", Dimension: 8}
}
func (e *embedSpy) Embed(ctx context.Context, texts []string) ([][]float32, error) {
	e.calls = append(e.calls, texts[0])
	return [][]float32{make([]float32, 8)}, nil
}

// helper: build a real EmbedService whose embedder is the spy. The
// VectorStore and EmbeddingRepo are also fakes; EmbedService.Embed will
// exercise them, but for ReembedService tests we only care about call counts.
func newReembedServiceForTest(t *testing.T, items []domain.ContextItem, spy *embedSpy) (*ReembedService, *fakeEmbedRepo) {
	t.Helper()
	repo := &fakeListRepo{items: items}
	embRepo := &fakeEmbedRepo{statusByItem: map[string]port.EmbeddingStatus{}}
	// EmbedService deps: embedder, vs, repo, fs, embRepo.
	// For reembed tests we don't actually care about Put or hydration
	// correctness; we only count Embed calls via spy.calls. Use a fake
	// vs that always succeeds and a fake fs that returns empty content.
	embedSvc := NewEmbedService(spy, &noopVectorStore{}, &getItemRepo{items: items}, &emptyFileStore{}, embRepo)
	return NewReembedService(repo, embedSvc, port.ModelInfo{Slug: "active-model", Dimension: 8}), embRepo
}

func TestReembedService_DryRunDoesNotEmbed(t *testing.T) {
	items := []domain.ContextItem{
		{ID: "i1", Title: "t1", Content: "c1"},
		{ID: "i2", Title: "t2", Content: "c2"},
	}
	svc, _ := newReembedServiceForTest(t, items, &embedSpy{errOn: nil})

	report, err := svc.Run(context.Background(), 0, true)
	require.NoError(t, err)
	assert.Equal(t, 2, report.Scanned)
	assert.Equal(t, 0, report.Embedded)
	assert.Equal(t, 0, report.Failed)
}

func TestReembedService_EmbedsAllItemsWhenNoneDone(t *testing.T) {
	items := []domain.ContextItem{
		{ID: "i1", Title: "t1", Content: "c1"},
		{ID: "i2", Title: "t2", Content: "c2"},
	}
	spy := &embedSpy{}
	svc, _ := newReembedServiceForTest(t, items, spy)

	report, err := svc.Run(context.Background(), 0, false)
	require.NoError(t, err)
	assert.Equal(t, 2, report.Scanned)
	assert.Equal(t, 2, report.Embedded)
	assert.Equal(t, 0, report.Failed)
}

func TestReembedService_SkipsItemsAlreadyDoneForActiveModel(t *testing.T) {
	items := []domain.ContextItem{
		{ID: "i1", Title: "t1", Content: "c1"},
		{ID: "i2", Title: "t2", Content: "c2"},
	}
	spy := &embedSpy{}
	svc, embRepo := newReembedServiceForTest(t, items, spy)
	// Mark i1 as already done under the active model.
	embRepo.statusByItem["i1"] = port.EmbeddingStatus{
		ItemID: "i1", ModelSlug: "active-model", Status: "done",
	}

	report, err := svc.Run(context.Background(), 0, false)
	require.NoError(t, err)
	assert.Equal(t, 1, report.Scanned, "i1 excluded by filter")
	assert.Equal(t, 1, report.Embedded)
}

func TestReembedService_ProcessesItemsDoneForOtherModelsOnly(t *testing.T) {
	// An item done under 'bge-m3' but not under active 'active-model'
	// must be re-embedded.
	items := []domain.ContextItem{{ID: "i1", Title: "t1", Content: "c1"}}
	spy := &embedSpy{}
	svc, embRepo := newReembedServiceForTest(t, items, spy)
	embRepo.statusByItem["i1"] = port.EmbeddingStatus{
		ItemID: "i1", ModelSlug: "bge-m3", Status: "done",
	}

	report, err := svc.Run(context.Background(), 0, false)
	require.NoError(t, err)
	assert.Equal(t, 1, report.Scanned)
	assert.Equal(t, 1, report.Embedded, "other-model done row does not exclude")
}

func TestReembedService_LimitHonored(t *testing.T) {
	items := []domain.ContextItem{
		{ID: "i1"}, {ID: "i2"}, {ID: "i3"},
	}
	spy := &embedSpy{}
	svc, _ := newReembedServiceForTest(t, items, spy)

	report, err := svc.Run(context.Background(), 2, false)
	require.NoError(t, err)
	assert.Equal(t, 2, report.Scanned)
}

func TestReembedService_FailureContinuesAndRecords(t *testing.T) {
	items := []domain.ContextItem{
		{ID: "ok", Title: "t-ok", Content: "c"},
		{ID: "boom", Title: "t-boom", Content: "c"},
		{ID: "ok2", Title: "t-ok2", Content: "c"},
	}
	// Custom spy that errors on the "boom" item.
	spy := &failingEmbedSpy{failOn: "t-boom\n\nc"}
	svc, _ := newReembedServiceForTest(t, items, nil)
	// Replace the embedder in the underlying EmbedService with our failing spy.
	// Easiest path: re-construct using the failing spy.
	embRepo := &fakeEmbedRepo{statusByItem: map[string]port.EmbeddingStatus{}}
	repo := &fakeListRepo{items: items}
	embedSvc := NewEmbedService(spy, &noopVectorStore{}, &getItemRepo{items: items}, &emptyFileStore{}, embRepo)
	svc = NewReembedService(repo, embedSvc, port.ModelInfo{Slug: "active-model", Dimension: 8})

	report, err := svc.Run(context.Background(), 0, false)
	require.NoError(t, err)
	assert.Equal(t, 3, report.Scanned)
	assert.Equal(t, 2, report.Embedded)
	assert.Equal(t, 1, report.Failed)
	require.Len(t, report.Failures, 1)
	assert.Contains(t, report.Failures[0].Error, "boom")
}

// failingEmbedSpy returns an error for any input containing failOn.
type failingEmbedSpy struct{ failOn string }

func (e *failingEmbedSpy) Model() port.ModelInfo { return port.ModelInfo{Slug: "active-model", Dimension: 8} }
func (e *failingEmbedSpy) Embed(ctx context.Context, texts []string) ([][]float32, error) {
	for _, t := range texts {
		if t == e.failOn {
			return nil, errors.New("boom: synthetic embed failure")
		}
	}
	return [][]float32{make([]float32, 8)}, nil
}

// noopVectorStore accepts all puts; never returns hits on search.
type noopVectorStore struct{}

func (noopVectorStore) Put(ctx context.Context, model, itemID string, v []float32) error {
	return nil
}
func (noopVectorStore) Search(ctx context.Context, q port.VectorQuery) ([]port.VectorHit, error) {
	return nil, nil
}
func (noopVectorStore) Delete(ctx context.Context, model, itemID string) error { return nil }

// emptyFileStore returns empty bytes for any URI.
type emptyFileStore struct{}

func (emptyFileStore) Put(name string, data []byte) (string, error) { return name, nil }
func (emptyFileStore) Get(uri string) ([]byte, error)              { return nil, nil }
func (emptyFileStore) Delete(uri string) error                     { return nil }

// getItemRepo returns canned items by ID; used so EmbedService.repo.Get
// works in tests.
type getItemRepo struct{ items []domain.ContextItem }

func (r *getItemRepo) Create(ctx context.Context, item domain.ContextItem) error { return nil }
func (r *getItemRepo) Update(ctx context.Context, item domain.ContextItem) error { return nil }
func (r *getItemRepo) Delete(ctx context.Context, id string) error               { return nil }
func (r *getItemRepo) Get(ctx context.Context, id string) (domain.ContextItem, error) {
	for _, it := range r.items {
		if it.ID == id {
			return it, nil
		}
	}
	return domain.ContextItem{}, errors.New("not found")
}
func (r *getItemRepo) List(ctx context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
	return r.items, "", nil
}

// fakeEmbedRepo mirrors the one in service/embed_test.go. If that file
// already declares a conflicting name, rename this one.
type fakeEmbedRepo struct {
	statusByItem map[string]port.EmbeddingStatus
	putCalls     int
}

func (f *fakeEmbedRepo) UpsertStatus(ctx context.Context, itemID, modelSlug, status, errStr string) error {
	f.putCalls++
	existing := f.statusByItem[itemID]
	existing.ItemID = itemID
	existing.ModelSlug = modelSlug
	existing.Status = status
	existing.Attempts++
	if status == "failed" {
		existing.LastError = errStr
	}
	f.statusByItem[itemID] = existing
	return nil
}
func (f *fakeEmbedRepo) GetStatus(ctx context.Context, itemID, modelSlug string) (port.EmbeddingStatus, error) {
	s, ok := f.statusByItem[itemID]
	if !ok {
		return port.EmbeddingStatus{}, errors.New("not found")
	}
	return s, nil
}
func (f *fakeEmbedRepo) ListFailed(ctx context.Context, limit int) ([]port.EmbeddingStatus, error) {
	return nil, nil
}
```

Note: the helper types (`fakeEmbedRepo`, `noopVectorStore`, `emptyFileStore`, `getItemRepo`) may collide with existing test types in `internal/service/embed_test.go` or `backfill_test.go`. Before writing the file, grep for those names; if any exist, reuse them instead of re-declaring. The implementer should resolve collisions during RED.

- [ ] **Step 2: Run tests to verify RED**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestReembedService -v ./internal/service/...`

Expected: compile failure (no `ReembedService`, no `NewReembedService`, no `ReembedReport`).

- [ ] **Step 3: Implement `internal/service/reembed.go`**

```go
package service

import (
	"context"
	"fmt"
	"os"

	"uni-context/internal/port"
)

// ReembedService bulk-embeds items under the active model. The filter
// differs from BackfillService: BackfillService targets items where
// any_embedding=0 (first-time embed), while ReembedService targets items
// that lack a status='done' row for the active model (migration to a new
// active model after `embed switch`).
//
// Idempotent: re-runs skip items already done for the active model.
// Resumable: failed items get status='failed' rows and are picked up by
// the worker (which is model-agnostic).
type ReembedService struct {
	repo   port.ContextRepo
	embed  *EmbedService
	active port.ModelInfo // slug of the currently-wired embedder
}

// NewReembedService wires the ContextRepo (lists candidate items) +
// EmbedService (embeds each) + the active model identifier (filter key).
func NewReembedService(repo port.ContextRepo, embed *EmbedService, active port.ModelInfo) *ReembedService {
	return &ReembedService{repo: repo, embed: embed, active: active}
}

// ReembedFailure records a per-item embed error. Aggregated in
// ReembedService.Run's report so the CLI can surface them.
type ReembedFailure struct {
	ItemID string
	Error  string
}

// ReembedReport summarizes one Run invocation. Scanned = candidates
// found (no done row for active model); Embedded = successful embeds;
// Failed = per-item failures.
type ReembedReport struct {
	Scanned  int
	Embedded int
	Failed   int
	Failures []ReembedFailure
}

// Run iterates items lacking a status='done' row for the active model
// and embeds each. For each item:
//   - dryRun=true: increment Scanned only.
//   - dryRun=false: call EmbedService.Embed; on failure record a
//     ReembedFailure and continue; on success increment Embedded.
//
// limit<=0 means no limit. Progress is logged to stderr every 100 items.
//
// Run does NOT return an error on per-item embed failures; the only error
// it returns is from the initial List call or ctx cancellation.
func (s *ReembedService) Run(ctx context.Context, limit int, dryRun bool) (ReembedReport, error) {
	var report ReembedReport

	items, _, err := s.repo.List(ctx, port.ItemFilter{
		NotDoneForModel: s.active.Slug,
		Limit:           limit,
	})
	if err != nil {
		return report, fmt.Errorf("list items pending for model %s: %w", s.active.Slug, err)
	}

	for i, item := range items {
		select {
		case <-ctx.Done():
			return report, ctx.Err()
		default:
		}

		report.Scanned++
		if dryRun {
			continue
		}

		if err := s.embed.Embed(ctx, item.ID, item.Title, item.Content); err != nil {
			report.Failed++
			report.Failures = append(report.Failures, ReembedFailure{
				ItemID: item.ID,
				Error:  err.Error(),
			})
			continue
		}
		report.Embedded++

		if (i+1)%100 == 0 {
			fmt.Fprintf(os.Stderr, "reembed: %d items processed\n", i+1)
		}
	}
	return report, nil
}
```

Note: this introduces a new `port.ItemFilter.NotDoneForModel string` field. Task 2 also extends `port.ItemFilter` + the sqlite `List` impl to honor it. See Step 4.

- [ ] **Step 4: Extend `port.ItemFilter` and sqlite `repo.List` to honor `NotDoneForModel`**

In `internal/port/repository.go` (locate the existing `ItemFilter` struct):

Add field:
```go
type ItemFilter struct {
    // ... existing fields ...
    // NotDoneForModel, when non-empty, restricts results to items that
    // lack a status='done' row in context_embedding for this model_slug.
    // Used by ReembedService to find items pending migration to a new
    // active model. Plan 2c addition.
    NotDoneForModel string
}
```

In `internal/adapter/sqlite/repo.go` `List` method, after the existing WHERE clauses are assembled, append:

```go
if f.NotDoneForModel != "" {
    whereParts = append(whereParts, `NOT EXISTS (
        SELECT 1 FROM context_embedding ce
        WHERE ce.item_id = ci.id
          AND ce.model_slug = ?
          AND ce.status = 'done'
    )`)
    args = append(args, f.NotDoneForModel)
}
```

Place this in the same WHERE-clause builder as the existing `AnyEmbedding` filter; the exact line range depends on the current `List` shape — read `internal/adapter/sqlite/repo.go` and insert alongside `AnyEmbedding`. If the implementer finds the structure too tangled, an alternative is to materialize the filter as a JOIN, but the `NOT EXISTS` subquery is preferred because it preserves the existing query plan.

- [ ] **Step 5: Run tests to verify GREEN**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestReembedService -v ./internal/service/...`

Expected: all `TestReembedService_*` tests PASS.

- [ ] **Step 6: Full repo build**

Run: `CGO_ENABLED=1 go build -tags sqlite_fts5 ./...`

Expected: still fails in `internal/app/app.go` (Task 3 territory) but NOT in any service file.

- [ ] **Step 7: goimports + commit**

```bash
goimports -w internal/service/reembed.go internal/service/reembed_test.go internal/port/repository.go internal/adapter/sqlite/repo.go
git add internal/service/reembed.go internal/service/reembed_test.go internal/port/repository.go internal/adapter/sqlite/repo.go
git commit -m "$(cat <<'EOF'
feat(service): add ReembedService for active-model migration

ReembedService bulk-embeds items lacking a status='done' row for the
active model. Mirrors BackfillService shape with a different filter
(migration vs first-time embed). Adds port.ItemFilter.NotDoneForModel
and the sqlite NOT EXISTS subquery to honor it.
EOF
)"
```

---

## Task 3: Wire reconciliation + active-model embedder construction

**Goal:** Update `app.Wire` to (a) construct a `ModelRegistry`, (b) run `reconcilePlan2cSync` on first Plan 2c invocation, (c) construct the embedder from the active model's provider/base_url/api_key rather than directly from `cfg.Embedder`. Removes the `EnsureModelRegistered` call.

**Files:**
- Modify: `internal/app/app.go` (rewrite the `embedder != nil` branch of `Wire`; add `reconcilePlan2cSync` function)
- Create: `internal/app/app_reconcile_test.go` (unit tests for `reconcilePlan2cSync`)

**Interfaces:**
- Consumes: `sqlite.NewModelRegistry`, `port.ModelRegistry`, `config.EmbedderConfig`, `domain.ErrNotFound`
- Produces: `app.reconcilePlan2cSync(ctx, db, reg, cfg) error` (unexported, called from `Wire`)

- [ ] **Step 1: Write the test file `internal/app/app_reconcile_test.go`**

```go
package app

import (
	"context"
	"database/sql"
	"testing"

	"uni-context/internal/config"
	"uni-context/internal/port"
	"uni-context/internal/adapter/sqlite"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func newReconcileDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })
	require.NoError(t, sqlite.Migrate(db))
	return db
}

func TestReconcilePlan2cSync_FreshDB_ConfigMatchesSeed(t *testing.T) {
	db := newReconcileDB(t)
	reg := sqlite.NewModelRegistry(db)
	ctx := context.Background()

	cfg := config.EmbedderConfig{
		Enabled: true, Provider: "ollama",
		BaseURL: "http://localhost:11434", Model: "bge-m3", Dimension: 1024,
	}
	require.NoError(t, reconcilePlan2cSync(ctx, db, reg, cfg))

	// plan_2c_synced flag set.
	var synced string
	require.NoError(t, db.QueryRow(
		`SELECT value FROM schema_meta WHERE key='plan_2c_synced'`).Scan(&synced))
	assert.Equal(t, "1", synced)

	// bge-m3 still default; config row's base_url updated to match cfg.
	active, err := reg.GetActive(ctx)
	require.NoError(t, err)
	assert.Equal(t, "bge-m3", active.Slug)
	assert.Equal(t, "http://localhost:11434", active.BaseURL)
}

func TestReconcilePlan2cSync_FreshDB_ConfigSlugDiffersFromSeed(t *testing.T) {
	db := newReconcileDB(t)
	reg := sqlite.NewModelRegistry(db)
	ctx := context.Background()

	cfg := config.EmbedderConfig{
		Enabled: true, Provider: "openai",
		BaseURL: "http://lmstudio:1234/v1", Model: "custom-slug", Dimension: 1024,
	}
	require.NoError(t, reconcilePlan2cSync(ctx, db, reg, cfg))

	// custom-slug now default; bge-m3 not.
	active, err := reg.GetActive(ctx)
	require.NoError(t, err)
	assert.Equal(t, "custom-slug", active.Slug)
	assert.Equal(t, "http://lmstudio:1234/v1", active.BaseURL)
}

func TestReconcilePlan2cSync_ExistingAliasRow_ConfigHeals(t *testing.T) {
	db := newReconcileDB(t)
	reg := sqlite.NewModelRegistry(db)
	ctx := context.Background()

	// Simulate Plan 2b's EnsureModelRegistered output: alias row with
	// config='{}' pointing at the seeded vec table.
	_, err := db.Exec(`
		INSERT INTO embedding_model
		    (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
		VALUES ('text-embedding-bge-m3', 'text-embedding-bge-m3', 'openai', 1024, 'vec_bge_m3_1024', 0, 'active', '{}', strftime('%s','now'))`)
	require.NoError(t, err)

	cfg := config.EmbedderConfig{
		Enabled: true, Provider: "openai",
		BaseURL: "http://lmstudio:1234/v1", Model: "text-embedding-bge-m3",
		Dimension: 1024,
	}
	require.NoError(t, reconcilePlan2cSync(ctx, db, reg, cfg))

	// No new row added (alias already existed).
	var n int
	require.NoError(t, db.QueryRow(
		`SELECT count(*) FROM embedding_model WHERE slug='text-embedding-bge-m3'`).Scan(&n))
	assert.Equal(t, 1, n)

	// Config healed; is_default flipped.
	active, err := reg.GetActive(ctx)
	require.NoError(t, err)
	assert.Equal(t, "text-embedding-bge-m3", active.Slug)
	assert.Equal(t, "openai", active.Provider)
	assert.Equal(t, "http://lmstudio:1234/v1", active.BaseURL)
	assert.Empty(t, active.APIKey)
}

func TestReconcilePlan2cSync_IdempotentOnRerun(t *testing.T) {
	db := newReconcileDB(t)
	reg := sqlite.NewModelRegistry(db)
	ctx := context.Background()

	cfg := config.EmbedderConfig{
		Enabled: true, Provider: "ollama",
		BaseURL: "http://localhost:11434", Model: "bge-m3", Dimension: 1024,
	}
	require.NoError(t, reconcilePlan2cSync(ctx, db, reg, cfg))

	// Mutate config to something different — second run must NOT apply it.
	cfg.Provider = "openai"
	cfg.BaseURL = "http://evil.example"
	require.NoError(t, reconcilePlan2cSync(ctx, db, reg, cfg))

	active, err := reg.GetActive(ctx)
	require.NoError(t, err)
	assert.Equal(t, "bge-m3", active.Slug)
	assert.Equal(t, "ollama", active.Provider, "second run ignored config; DB authoritative")
	assert.Equal(t, "http://localhost:11434", active.BaseURL)
}

// Sanity: registry + reconcile compose to produce a usable active descriptor.
func TestReconcilePlan2cSync_ProducesUsableActiveDescriptor(t *testing.T) {
	db := newReconcileDB(t)
	reg := sqlite.NewModelRegistry(db)
	ctx := context.Background()

	cfg := config.EmbedderConfig{
		Enabled: true, Provider: "openai",
		BaseURL: "https://api.openai.com/v1", APIKey: "sk-xyz",
		Model: "text-embedding-3-small", Dimension: 1536,
	}
	require.NoError(t, reconcilePlan2cSync(ctx, db, reg, cfg))

	active, err := reg.GetActive(ctx)
	require.NoError(t, err)
	assert.Equal(t, port.ModelDescriptor{
		Slug: "text-embedding-3-small", Name: "text-embedding-3-small",
		Provider: "openai", BaseURL: "https://api.openai.com/v1",
		APIKey: "sk-xyz", Dimension: 1536,
		VecTable: "vec_text_embedding_3_small_1536", IsDefault: true,
		Status: "active",
	}, active)
}
```

- [ ] **Step 2: Run tests to verify RED**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestReconcilePlan2cSync -v ./internal/app/...`

Expected: compile failure — `reconcilePlan2cSync` undefined.

- [ ] **Step 3: Implement `reconcilePlan2cSync` in `internal/app/app.go`**

Add the function near the bottom of `app.go` (after `mkdirp`):

```go
// reconcilePlan2cSync runs once on first Plan 2c Wire invocation.
// gated by schema_meta.plan_2c_synced. After first run, DB is authoritative
// and cfg.Embedder (except `enabled`) is ignored — `embed switch` becomes
// the only way to change the active model.
//
// Behavior:
//  1. If plan_2c_synced == '1', return immediately.
//  2. If cfg.Embedder.Model not in DB: Register from cfg.Embedder fields.
//     If exists: UpdateConfig to overwrite provider + config JSON with
//     cfg.Embedder values (heals Plan 2b alias rows whose config was '{}').
//  3. SetDefault(cfg.Embedder.Model) — atomic flip; idempotent if already default.
//  4. INSERT OR REPLACE schema_meta plan_2c_synced = '1'.
func reconcilePlan2cSync(ctx context.Context, db *sql.DB, reg port.ModelRegistry, cfg config.EmbedderConfig) error {
	var synced string
	err := db.QueryRowContext(ctx,
		`SELECT value FROM schema_meta WHERE key = 'plan_2c_synced'`).Scan(&synced)
	if err == nil && synced == "1" {
		return nil
	}
	if err != nil && err != sql.ErrNoRows {
		return fmt.Errorf("read plan_2c_synced flag: %w", err)
	}

	_, getErr := reg.Get(ctx, cfg.Model)
	switch {
	case getErr == nil:
		// Row exists: heal config from cfg.Embedder.
		if err := reg.UpdateConfig(ctx, cfg.Model, cfg.BaseURL, cfg.APIKey, cfg.Provider); err != nil {
			return fmt.Errorf("heal config for %s: %w", cfg.Model, err)
		}
	case errors.Is(getErr, domain.ErrNotFound):
		// Row missing: register fresh.
		if err := reg.Register(ctx, port.ModelSpec{
			Slug: cfg.Model, Provider: cfg.Provider,
			BaseURL: cfg.BaseURL, APIKey: cfg.APIKey, Dimension: cfg.Dimension,
		}); err != nil {
			return fmt.Errorf("register %s: %w", cfg.Model, err)
		}
	default:
		return fmt.Errorf("lookup %s: %w", cfg.Model, getErr)
	}

	if err := reg.SetDefault(ctx, cfg.Model); err != nil {
		return fmt.Errorf("set default %s: %w", cfg.Model, err)
	}

	if _, err := db.ExecContext(ctx,
		`INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('plan_2c_synced', '1')`); err != nil {
		return fmt.Errorf("set plan_2c_synced flag: %w", err)
	}
	return nil
}
```

Add imports as needed: `"errors"`, `"database/sql"` (if not already imported — it is, for `*sql.DB`).

- [ ] **Step 4: Rewrite the embedder branch of `Wire`**

In `internal/app/app.go`, locate the existing branch:

```go
if cfg.Embedder.Enabled {
    switch cfg.Embedder.Provider {
    case "ollama":
        embedder = ollama.New(...)
    case "openai":
        embedder = openai.New(...)
    default:
        return nil, fmt.Errorf("unsupported embedder provider: %s", cfg.Embedder.Provider)
    }
    if err := sqlite.EnsureModelRegistered(db, ...); err != nil {
        return nil, fmt.Errorf("register embedder model: %w", err)
    }
    vectorStore := sqlite.NewVectorStore(db)
    embeddingRepo = sqlite.NewEmbeddingRepo(db)
    embedSvc = service.NewEmbedService(embedder, vectorStore, repo, fs, embeddingRepo)
    backfill = service.NewBackfillService(repo, embedSvc)
    worker = service.NewWorkerService(repo, embeddingRepo, embedSvc)
}
```

Replace with:

```go
if cfg.Embedder.Enabled {
    registry := sqlite.NewModelRegistry(db)

    // First-Plan-2c-run reconciliation. After this, DB is authoritative
    // and `embed switch` is the only way to change the active model.
    if err := reconcilePlan2cSync(context.Background(), db, registry, cfg.Embedder); err != nil {
        _ = db.Close()
        return nil, fmt.Errorf("plan 2c reconcile: %w", err)
    }

    active, err := registry.GetActive(context.Background())
    if err != nil {
        _ = db.Close()
        return nil, fmt.Errorf("read active model: %w", err)
    }

    // Construct embedder for the active model's provider + config.
    switch active.Provider {
    case "ollama":
        embedder = ollama.New(active.BaseURL, active.Slug, active.Dimension)
    case "openai":
        embedder = openai.New(active.BaseURL, active.Slug, active.Dimension, active.APIKey)
    default:
        _ = db.Close()
        return nil, fmt.Errorf("unsupported provider %q for active model %q",
            active.Provider, active.Slug)
    }

    vectorStore := sqlite.NewVectorStore(db)
    embeddingRepo = sqlite.NewEmbeddingRepo(db)
    embedSvc = service.NewEmbedService(embedder, vectorStore, repo, fs, embeddingRepo)
    backfill = service.NewBackfillService(repo, embedSvc)
    worker = service.NewWorkerService(repo, embeddingRepo, embedSvc)
    reembed = service.NewReembedService(repo, embedSvc, port.ModelInfo{
        Slug: active.Slug, Dimension: active.Dimension,
    })
}
```

Also extend the function-scope `var` declarations near the top of `Wire`:

```go
var (
    // ... existing decls ...
    reembed *service.ReembedService
)
```

And add `Reembed` to the `App` struct:

```go
type App struct {
    // ... existing fields ...
    Reembed *service.ReembedService // Plan 2c: bulk re-embed under active model
}
```

And in the return struct literal:

```go
return &App{
    // ... existing fields ...
    Reembed: reembed,
}, nil
```

- [ ] **Step 5: Run tests to verify GREEN**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestReconcilePlan2cSync -v ./internal/app/...`

Expected: all `TestReconcilePlan2cSync_*` tests PASS.

- [ ] **Step 6: Full repo build**

Run: `CGO_ENABLED=1 go build -tags sqlite_fts5 ./...`

Expected: SUCCESS. The `EnsureModelRegistered` reference is gone; `app.Wire` now uses `registry`.

- [ ] **Step 7: Run the full test suite**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./...`

Expected: all packages pass. Pay attention to:
- `internal/adapter/sqlite` — confirms vec smoke tests still work with new ModelRegistry.
- `internal/cli` — confirms existing `embed backfill/worker` tests still pass.
- `internal/service` — confirms EmbedService / BackfillService tests unaffected.

- [ ] **Step 8: goimports + commit**

```bash
goimports -w internal/app/app.go internal/app/app_reconcile_test.go
git add internal/app/app.go internal/app/app_reconcile_test.go
git commit -m "$(cat <<'EOF'
feat(app): wire ModelRegistry; add reconcilePlan2cSync

Wire now constructs a ModelRegistry, runs first-Plan-2c-run reconciliation
(heal existing rows / register new) gated by schema_meta, and constructs
the embedder from the active model descriptor instead of cfg.Embedder
directly. Exposes Reembed on the App struct for the CLI to consume.

Removes the last EnsureModelRegistered reference; the placeholder function
was deleted in Task 1.
EOF
)"
```

---

## Task 4: CLI `embed model` subcommands

**Goal:** Wire `unictx embed model add/list/remove` to the `ModelRegistry` exposed on `App`. Add the `embed model` parent (no Run). Cobra auto-handles parent-help.

**Files:**
- Modify: `internal/cli/embed.go` (add `embedModelCmd` + three children + flag bindings + `init()` registration)
- Modify: `internal/cli/embed_test.go` (extend structural test)

**Interfaces:**
- Consumes: `App.Registry` (added in this task), `port.ModelSpec`, `cobra` flag bindings
- Produces: `embedModelCmd`, `embedModelAddCmd`, `embedModelListCmd`, `embedModelRemoveCmd` package-level vars

- [ ] **Step 1: Extend the `App` struct with `Registry`**

In `internal/app/app.go`:

```go
type App struct {
    // ... existing fields ...
    // Registry is non-nil when cfg.Embedder.Enabled is true. CLI uses it
    // for `embed model add/list/remove` and `embed switch`. Plan 2c.
    Registry port.ModelRegistry
}
```

In `Wire`'s return literal, set `Registry: registry` inside the embedder-enabled branch (it stays nil otherwise, which the CLI guards against).

- [ ] **Step 2: Update the structural test in `internal/cli/embed_test.go`**

The existing test asserts `embed` has 2 subcommands. After this task `embed` will have 4 (backfill, worker, model, + switch from Task 5 + reembed from Task 5 = 5 total at end of Task 5). For Task 4 specifically, the count is 3 (backfill, worker, model).

Update the assertion to be future-proof by checking command names rather than count:

```go
func TestEmbedCmd_HasExpectedSubcommands(t *testing.T) {
    subs := embedCmd.Commands()
    names := []string{}
    for _, c := range subs {
        names = append(names, c.Use)
    }
    for _, want := range []string{"backfill", "worker", "model"} {
        assert.Contains(t, names, want, "embed must expose %q subcommand", want)
    }
}
```

(Keep using `assert.Contains` so future tasks adding `switch`/`reembed` don't break this test.)

- [ ] **Step 3: Write `TestEmbedModelCmd_AddParsesFlags`**

Add to `internal/cli/embed_test.go`:

```go
func TestEmbedModelCmd_AddParsesFlags(t *testing.T) {
    // Structural: confirm flags exist on the add subcommand.
    addCmd, _, err := (&cobra.Command{}).Find([]string{"model", "add"})
    // The root command isn't reachable from a bare &cobra.Command{}; use
    // the package-level embedCmd directly instead.
    _ = addCmd
    _ = err

    sub := findSub(embedCmd, "model")
    require.NotNil(t, sub)
    addSub := findSub(sub, "add")
    require.NotNil(t, addSub)

    for _, flag := range []string{"provider", "base-url", "dim", "api-key"} {
        assert.NotNil(t, addSub.Flags().Lookup(flag),
            "add command must expose --%s flag", flag)
    }
}

func findSub(parent *cobra.Command, use string) *cobra.Command {
    for _, c := range parent.Commands() {
        if c.Use == use {
            return c
        }
    }
    return nil
}
```

Add `cobra` import: `"github.com/spf13/cobra"`. Add `require` import if missing.

- [ ] **Step 4: Run tests to verify RED**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run 'TestEmbedCmd_HasExpectedSubcommands|TestEmbedModelCmd_AddParsesFlags' -v ./internal/cli/...`

Expected: FAIL — `embed model` and `embed model add` don't exist yet.

- [ ] **Step 5: Implement the `embed model` subcommands in `internal/cli/embed.go`**

Append to `internal/cli/embed.go`:

```go
// embedModelCmd is the parent for model-lifecycle subcommands. No RunE:
// invoking `unictx embed model` without a subcommand prints cobra help.
var embedModelCmd = &cobra.Command{
    Use:   "model",
    Short: "Manage embedding models (add/list/remove)",
}

// Flags for `embed model add`.
var (
    modelAddProvider string
    modelAddBaseURL  string
    modelAddDim      int
    modelAddAPIKey   string
)

var embedModelAddCmd = &cobra.Command{
    Use:   "add <slug>",
    Short: "Register a new embedding model (creates its vec table)",
    Args:  cobra.ExactArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        a, _, err := loadApp()
        if err != nil {
            return err
        }
        defer a.DB.Close()
        if a.Registry == nil {
            return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
        }

        slug := args[0]
        return a.Registry.Register(cmd.Context(), port.ModelSpec{
            Slug:      slug,
            Provider:  modelAddProvider,
            BaseURL:   modelAddBaseURL,
            APIKey:    modelAddAPIKey,
            Dimension: modelAddDim,
        })
    },
}

var embedModelListCmd = &cobra.Command{
    Use:   "list",
    Short: "List all registered embedding models",
    Args:  cobra.NoArgs,
    RunE: func(cmd *cobra.Command, args []string) error {
        a, _, err := loadApp()
        if err != nil {
            return err
        }
        defer a.DB.Close()
        if a.Registry == nil {
            return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
        }

        models, err := a.Registry.List(cmd.Context())
        if err != nil {
            return err
        }
        w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
        fmt.Fprintln(w, "SLUG\tPROVIDER\tDIM\tVEC_TABLE\tDEFAULT\tSTATUS")
        for _, m := range models {
            defaultMark := ""
            if m.IsDefault {
                defaultMark = "*"
            }
            fmt.Fprintf(w, "%s\t%s\t%d\t%s\t%s\t%s\n",
                m.Slug, m.Provider, m.Dimension, m.VecTable, defaultMark, m.Status)
        }
        return w.Flush()
    },
}

var embedModelRemoveCmd = &cobra.Command{
    Use:   "remove <slug>",
    Short: "Drop a model's vec table + delete its row (refuses default + shared)",
    Args:  cobra.ExactArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        a, _, err := loadApp()
        if err != nil {
            return err
        }
        defer a.DB.Close()
        if a.Registry == nil {
            return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
        }

        return a.Registry.Remove(cmd.Context(), args[0])
    },
}
```

Update `init()` to register the new commands:

```go
func init() {
    // ... existing flag bindings ...

    embedModelAddCmd.Flags().StringVar(&modelAddProvider, "provider", "",
        "embedder provider (ollama|openai)")
    embedModelAddCmd.Flags().StringVar(&modelAddBaseURL, "base-url", "",
        "embedder base URL (e.g. http://localhost:11434 or https://api.openai.com/v1)")
    embedModelAddCmd.Flags().IntVar(&modelAddDim, "dim", 0,
        "embedding dimension (must match the model's output dim)")
    embedModelAddCmd.Flags().StringVar(&modelAddAPIKey, "api-key", "",
        "API key (required for OpenAI hosted; local servers ignore)")

    embedModelCmd.AddCommand(embedModelAddCmd)
    embedModelCmd.AddCommand(embedModelListCmd)
    embedModelCmd.AddCommand(embedModelRemoveCmd)

    embedCmd.AddCommand(embedBackfillCmd)
    embedCmd.AddCommand(embedWorkerCmd)
    embedCmd.AddCommand(embedModelCmd) // Plan 2c
    rootCmd.AddCommand(embedCmd)
}
```

Add imports: `"text/tabwriter"`, `"uni-context/internal/port"`.

- [ ] **Step 6: Run tests to verify GREEN**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run 'TestEmbedCmd_HasExpectedSubcommands|TestEmbedModelCmd_AddParsesFlags' -v ./internal/cli/...`

Expected: PASS.

- [ ] **Step 7: Build full repo**

Run: `CGO_ENABLED=1 go build -tags sqlite_fts5 ./...`

Expected: SUCCESS.

- [ ] **Step 8: goimports + commit**

```bash
goimports -w internal/cli/embed.go internal/cli/embed_test.go internal/app/app.go
git add internal/cli/embed.go internal/cli/embed_test.go internal/app/app.go
git commit -m "$(cat <<'EOF'
feat(cli): add 'embed model add/list/remove' subcommands

Wires the ModelRegistry to the CLI under a new 'embed model' parent.
Add takes provider/base_url/dim/api_key flags and creates a per-slug
vec table. List prints a tabular view. Remove refuses default models
and shared vec_tables (delegated to the registry).
EOF
)"
```

---

## Task 5: CLI `embed switch` + `embed reembed`

**Goal:** Wire the remaining two CLI commands. `embed switch <slug>` flips is_default atomically + prints a stderr reminder to run reembed. `embed reembed [--limit] [--dry-run]` invokes `ReembedService.Run`.

**Files:**
- Modify: `internal/cli/embed.go` (add `embedSwitchCmd`, `embedReembedCmd`, flag bindings, `init()` registration)
- Modify: `internal/cli/embed_test.go` (extend structural test)

**Interfaces:**
- Consumes: `App.Registry.SetDefault`, `App.Reembed.Run`
- Produces: `embedSwitchCmd`, `embedReembedCmd` package-level vars

- [ ] **Step 1: Update structural test to also cover switch + reembed**

In `internal/cli/embed_test.go`:

```go
func TestEmbedCmd_HasExpectedSubcommands(t *testing.T) {
    subs := embedCmd.Commands()
    names := []string{}
    for _, c := range subs {
        names = append(names, c.Use)
    }
    for _, want := range []string{"backfill", "worker", "model", "switch", "reembed"} {
        assert.Contains(t, names, want, "embed must expose %q subcommand", want)
    }
}

func TestEmbedReembedCmd_HasLimitAndDryRunFlags(t *testing.T) {
    sub := findSub(embedCmd, "reembed")
    require.NotNil(t, sub)
    assert.NotNil(t, sub.Flags().Lookup("limit"))
    assert.NotNil(t, sub.Flags().Lookup("dry-run"))
}
```

- [ ] **Step 2: Run tests to verify RED**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run 'TestEmbedCmd_HasExpectedSubcommands|TestEmbedReembedCmd' -v ./internal/cli/...`

Expected: FAIL — switch + reembed don't exist yet.

- [ ] **Step 3: Implement `embedSwitchCmd` and `embedReembedCmd`**

Append to `internal/cli/embed.go`:

```go
var (
    reembedLimit  int
    reembedDryRun bool
)

var embedSwitchCmd = &cobra.Command{
    Use:   "switch <slug>",
    Short: "Set a registered model as the active default (atomic)",
    Args:  cobra.ExactArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        a, _, err := loadApp()
        if err != nil {
            return err
        }
        defer a.DB.Close()
        if a.Registry == nil {
            return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
        }

        slug := args[0]
        if err := a.Registry.SetDefault(cmd.Context(), slug); err != nil {
            return err
        }
        fmt.Fprintf(os.Stderr,
            "Active model switched to %s. Run 'unictx embed reembed' to migrate existing items.\n",
            slug)
        return nil
    },
}

var embedReembedCmd = &cobra.Command{
    Use:   "reembed",
    Short: "Re-embed items lacking a done status row for the active model",
    RunE: func(cmd *cobra.Command, args []string) error {
        a, _, err := loadApp()
        if err != nil {
            return err
        }
        defer a.DB.Close()
        if a.Reembed == nil {
            return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
        }

        ctx := signalContext()
        report, err := a.Reembed.Run(ctx, reembedLimit, reembedDryRun)
        if err != nil {
            return err
        }

        if reembedDryRun {
            fmt.Printf("dry run: would re-embed %d items\n", report.Scanned)
            return nil
        }
        fmt.Printf("reembed complete: embedded=%d failed=%d scanned=%d\n",
            report.Embedded, report.Failed, report.Scanned)
        if len(report.Failures) > 0 {
            fmt.Println("failures:")
            for _, f := range report.Failures {
                fmt.Printf("  %s: %s\n", f.ItemID, f.Error)
            }
        }
        return nil
    },
}
```

Update `init()`:

```go
func init() {
    // ... existing flag bindings ...

    embedReembedCmd.Flags().IntVar(&reembedLimit, "limit", 0,
        "max items to embed (0 = no limit)")
    embedReembedCmd.Flags().BoolVar(&reembedDryRun, "dry-run", false,
        "count candidates without embedding")

    // ... existing AddCommand calls ...
    embedCmd.AddCommand(embedSwitchCmd)
    embedCmd.AddCommand(embedReembedCmd)
    rootCmd.AddCommand(embedCmd)
}
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run 'TestEmbedCmd_HasExpectedSubcommands|TestEmbedReembedCmd' -v ./internal/cli/...`

Expected: PASS.

- [ ] **Step 5: Build + run full test suite**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./...`

Expected: all packages PASS. Pay attention to:
- `internal/cli/embed_test.go` structural test (now expects 5 subcommands by name).
- No regression in `e2e_test.go` / `e2e_backfill_test.go` (those use separate binaries; should be unaffected).

- [ ] **Step 6: goimports + commit**

```bash
goimports -w internal/cli/embed.go internal/cli/embed_test.go
git add internal/cli/embed.go internal/cli/embed_test.go
git commit -m "$(cat <<'EOF'
feat(cli): add 'embed switch' and 'embed reembed' subcommands

'switch <slug>' atomically flips is_default and prints a stderr reminder
to run reembed. 'reembed [--limit N] [--dry-run]' invokes ReembedService
to bulk-embed items lacking a done status row for the active model.
EOF
)"
```

---

## Task 6: CHANGELOG + end-to-end smoke

**Goal:** Document Plan 2c in CHANGELOG (including the API-key-in-DB caveat). Run an end-to-end smoke against a fresh in-memory DB to confirm the registry commands work together. No new migration; no new production code.

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: everything from Tasks 1-5
- Produces: CHANGELOG section, no code

- [ ] **Step 1: Append Plan 2c section to CHANGELOG.md**

Insert before the "## Bugfix — OpenAI adapter..." section (or after Plan 2b, keeping chronological order):

```markdown
## Plan 2c — Multi-Model Registry & Migration (2026-06-21)

Replaces the Plan 2a `EnsureModelRegistered` placeholder with a runtime
model registry. Adds CLI commands for model lifecycle and migration.
The active model's source of truth moves from `config.Embedder.Model`
to `embedding_model.is_default`, with a one-time reconciliation on
first Plan 2c run. See
`docs/superpowers/specs/2026-06-21-plan-2c-multi-model-registry-design.md`
for the design.

**What shipped:**
- **`port.ModelRegistry` + sqlite impl:** add/get/list/default/update/
  remove with transactional DDL for per-slug `vec_<slug>_<dim>` tables.
  Shared-table protection preserves Plan 2b alias rows.
- **`unictx embed model add/list/remove`:** CLI surface for the registry.
- **`unictx embed switch <slug>`:** atomic is_default flip; prints a stderr
  reminder to follow with `embed reembed`.
- **`unictx embed reembed [--limit N] [--dry-run]`:** bulk re-embed items
  lacking a status='done' row for the active model. Reuses Plan 2b's
  status mechanism; resumable.
- **`service.ReembedService`:** Plan 2c sibling of BackfillService with a
  different filter (`NotDoneForModel` vs `AnyEmbedding=0`).
- **`app.Wire` reconciliation:** first Plan 2c run heals/registers the
  active model from `cfg.Embedder`, gated by
  `schema_meta.plan_2c_synced`. Subsequent runs trust the DB; the only
  way to change the active model is `embed switch`.

### Known Limitations (Plan 2c)

1. **API keys persist in `embedding_model.config` JSON.** The DB file
   contains them in plaintext. Set `unictx.db` permissions to 0600 on
   shared systems. OS keychain integration is a future plan.

2. **Only one active model at a time.** Parallel embedding (N models per
   item) is Plan 2d. Adding a model + switching requires `embed reembed`
   before vector search returns hits for existing items.

3. **`embed model remove` refuses shared vec_tables.** Plan 2b alias
   rows can share a vec_table with the seed; the registry detects this
   and errors out, requiring dependents to be removed first.

4. **Reconciliation runs once.** After `plan_2c_synced=1` is set, editing
   `embedder.model` in config has no effect. Use `embed switch`.

5. **Migration transition state.** Between `embed switch` and
   `embed reembed` completing, vector search returns 0 hits for the new
   model (SearchService hybrid mode falls back to fts-only gracefully).

### Deferred to Plan 2d+

- Parallel embedding (N models per item)
- Per-call model parameter on `EmbedService.Embed`
- Provider auto-detection (probe `/v1/models` endpoint)
- OpenAI batched embeddings API (1 request, N inputs)
- `unictx embed status <id>` (read-only status inspection)
- Migrating Plan 2b alias rows to per-slug vec tables
```

- [ ] **Step 2: End-to-end smoke against a fresh temp DB**

Run a manual smoke to confirm the registry commands work together. Use a temp XDG root so the user's real DB is untouched.

```bash
TESTROOT=$(mktemp -d /tmp/unictx-plan2c.XXXXXX)
mkdir -p "$TESTROOT/config/unictx" "$TESTROOT/data"
cat > "$TESTROOT/config/unictx/config.yaml" <<'YAML'
embedder:
  enabled: true
  provider: openai
  base_url: http://100.126.178.61:1234/v1
  model: text-embedding-bge-m3
  dimension: 1024
YAML

export XDG_CONFIG_HOME="$TESTROOT/config"
export XDG_DATA_HOME="$TESTROOT/data"

make build

./unictx doctor
# Expected: schema version 3, embedder OK.

./unictx embed model list
# Expected: text-embedding-bge-m3 row, is_default=* (reconciled from config).

./unictx embed model add test-model --provider openai \
    --base-url http://100.126.178.61:1234/v1 --dim 1024
# Expected: exits 0; new row appears in `embed model list` with is_default blank.

./unictx embed switch test-model
# Expected: stderr "Active model switched to test-model..." + reminder.

./unictx embed model list
# Expected: test-model now has is_default=*; text-embedding-bge-m3 is_default blank.

./unictx embed model remove text-embedding-bge-m3
# Expected: FAILS with "vec table vec_bge_m3_1024 shared by 2 models" — both
# the seed bge-m3 row and text-embedding-bge-m3 point at it. Plan 2b alias
# protection working as designed.

./unictx embed switch text-embedding-bge-m3
./unictx embed model remove test-model
# Expected: succeeds (test-model had its own vec_test_model_1024).

unset XDG_CONFIG_HOME XDG_DATA_HOME
rm -rf "$TESTROOT"
```

If any step fails, debug + fix before committing. The shared-vec-table refusal on `remove text-embedding-bge-m3` is **expected behavior** — it confirms the Plan 2b alias protection works.

- [ ] **Step 3: Commit CHANGELOG**

```bash
git add CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs(changelog): document Plan 2c multi-model registry

Records what shipped, the 5 known limitations (API key persistence,
single-active semantics, shared vec_table protection, one-shot
reconciliation, migration transition state), and what's deferred to
Plan 2d+.
EOF
)"
```

---

## Self-Review

### Spec coverage check

| Spec section | Implementing task |
|---|---|
| Motivation (3 pain points) | All (EnsureModelRegistered removed T1; CLI surface T4+T5; migration path T5+T2) |
| Scope in: ModelRegistry port + impl | T1 |
| Scope in: per-slug vec tables via DDL | T1 (Register creates them) |
| Scope in: CLI `add/list/remove/switch/reembed` | T4 (add/list/remove) + T5 (switch/reembed) |
| Scope in: first-run reconciliation | T3 (reconcilePlan2cSync) |
| Scope in: ReembedService | T2 |
| Architecture: one active embedder wired from DB | T3 (Wire rewrite) |
| Key principle: `port.Embedder` unchanged | All (none of the tasks touch it) ✓ |
| Key principle: `EmbedService.Embed` signature unchanged | All (none modify it) ✓ |
| Components `port/modelregistry.go` | T1 |
| Components `adapter/sqlite/model_registry.go` rewrite | T1 |
| Components `app/app.go` Wire changes | T3 |
| Components `service/reembed.go` | T2 |
| Components `cli/embed.go` extend | T4 + T5 |
| Components unchanged list | All tasks respect ✓ |
| Flow A (fresh install) | T3 (TestReconcilePlan2cSync_FreshDB_*) |
| Flow B (existing Plan 2b user upgrades) | T3 (TestReconcilePlan2cSync_ExistingAliasRow_*) |
| Flow C (add new model) | T1 (TestModelRegistry_Register_*) + T4 (CLI) + Task 6 smoke |
| Flow D (switch) | T1 (TestModelRegistry_SetDefault_*) + T5 (CLI) + Task 6 smoke |
| Flow E (reembed) | T2 (TestReembedService_*) + T5 (CLI) |
| Flow F (remove) | T1 (TestModelRegistry_Remove_*) + T4 (CLI) + Task 6 smoke |
| Error handling matrix | Covered by per-task tests (slug exists, unknown slug, shared vec_table, default rejection) |
| Plan 2b alias sharing protection | T1 (Remove rejects shared vec_table) + Task 6 smoke verifies |
| Testing plan rows | All implemented as unit tests; integration = Task 6 smoke |
| Risks (6 items) | Mitigutions baked into the code (schema_meta gate, 0600 warning in CHANGELOG, shared-table check, stderr reminder, idempotency) |
| Out of scope | None of the tasks implement deferred items ✓ |

### Placeholder scan

- No "TBD", "TODO", "implement later" in the plan.
- All test code is complete (not "write tests for the above").
- All implementation code blocks are complete (not "add error handling").
- Step 4 of Task 2 instructs the implementer to read `repo.go` and locate the right insertion point — this is necessary because the line range isn't stable across future edits, but the instruction gives the exact SQL and field name to add. Acceptable.

### Type consistency check

- `port.ModelSpec{Slug, Provider, BaseURL, APIKey string; Dimension int}` — defined T1, used T1 (Register), T3 (reconcile). ✓
- `port.ModelDescriptor{Slug, Name, Provider, BaseURL, APIKey string; Dimension int; VecTable string; IsDefault bool; Status string}` — defined T1, used T1, T3, T4. ✓
- `port.ModelRegistry` methods — defined T1, used T3 (Get, UpdateConfig, Register, SetDefault, GetActive), T4 (Register, List, Remove), T5 (SetDefault). ✓
- `port.ModelInfo{Slug string; Dimension int}` — already exists from Plan 2a, used T2 (ReembedService.active), T3 (constructed from active descriptor). ✓
- `service.ReembedService` + `Run(ctx, limit, dryRun) (ReembedReport, error)` — defined T2, used T5 (CLI calls `a.Reembed.Run`). ✓
- `service.ReembedReport{Scanned, Embedded, Failed int; Failures []ReembedFailure}` — defined T2, used T5. ✓
- `service.ReembedFailure{ItemID, Error string}` — defined T2, used T5. ✓
- `App.Registry port.ModelRegistry` — added T4, used T4 + T5. ✓
- `App.Reembed *service.ReembedService` — added T3, used T5. ✓
- `port.ItemFilter.NotDoneForModel string` — added T2, used T2. ✓

All type references resolve. No drift.
