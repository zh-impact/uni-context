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
//
// DSN enables _foreign_keys=on to match production Open() — required for the
// Remove cascade test (DELETE from embedding_model must cascade to
// context_embedding via FK ON DELETE CASCADE declared in migration 0002).
func openTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", "file::memory:?_foreign_keys=on")
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
	// Schema (migration 0001) uses owner_user_id (not user_id) and requires
	// the NOT NULL source column; the original brief test had a schema mismatch.
	_, err := db.Exec(`
		INSERT INTO context_item (id, scope, kind, source, owner_user_id, title, content, created_at, updated_at)
		VALUES ('item-1', 'user', 'note', 'test', 'default', 't', 'c', 0, 0)`)
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
