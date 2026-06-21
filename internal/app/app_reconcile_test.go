package app

import (
	"context"
	"database/sql"
	"testing"

	"uni-context/internal/adapter/sqlite"
	"uni-context/internal/config"
	"uni-context/internal/port"

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
