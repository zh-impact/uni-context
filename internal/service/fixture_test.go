package service

import (
	"database/sql"
	"fmt"
	"path/filepath"
	"testing"

	"uni-context/internal/adapter/fsstore"
	sqlite "uni-context/internal/adapter/sqlite"
	"uni-context/internal/port"

	"github.com/stretchr/testify/require"
)

// fakeRepo is an in-memory port.ContextRepo for service tests.
// (For adapter-level tests, see the sqlite package.)
// We hand-roll this rather than use a mock generator — the interface is small.

type ingestFixture struct {
	repo   *fakeRepo
	fs     port.FileStore
	fsRoot string
	svc    *IngestService
}

func newIngestFixture(t *testing.T) *ingestFixture {
	t.Helper()
	repo := newFakeRepo()
	root := filepath.Join(t.TempDir(), "fs")
	fs, err := fsstore.New(root)
	require.NoError(t, err)
	return &ingestFixture{
		repo:   repo,
		fs:     fs,
		fsRoot: root,
		svc:    NewIngestService(repo, fs),
	}
}

// newMemFileStore returns a FileStore backed by a temp dir. Used by
// ingest tests that build a custom IngestService (e.g. the embed-wired
// variant) rather than the full ingestFixture.
func newMemFileStore(t *testing.T) port.FileStore {
	t.Helper()
	fs, err := fsstore.New(filepath.Join(t.TempDir(), "fs"))
	require.NoError(t, err)
	return fs
}

// newMemEmbeddingRepo returns a SQLite-backed EmbeddingRepo sharing the
// same *sql.DB as the caller's repo. The shared-DB requirement is load
// bearing: EmbedService writes status rows that tests then read back via
// the same handle. A separate :memory: database would be invisible.
func newMemEmbeddingRepo(t *testing.T, db *sql.DB) port.EmbeddingRepo {
	t.Helper()
	return sqlite.NewEmbeddingRepo(db)
}

// newMemVectorStore opens an in-memory SQLite DB, runs migrations
// (registers vec0 via the sqlite package init), and registers a
// "fake-model" row (8-dim) so the service-layer tests can use
// fake.New("fake-model", 8) without depending on the production bge-m3
// (1024-dim) seed.
//
// The migration 0002 only seeds bge-m3; service tests want a cheap 8-dim
// model, so we register a parallel vec0 table + embedding_model row here.
// Returns the VectorStore, a SQLite-backed ContextRepo (so VectorStore's
// JOIN on context_item resolves — fakeRepo is in-memory and invisible to
// SQLite), and the underlying *sql.DB (caller closes).
func newMemVectorStore(t *testing.T) (port.VectorStore, port.ContextRepo, *sql.DB) {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	require.NoError(t, sqlite.Migrate(db))

	// Register fake-model: 8-dim vec0 table + embedding_model row.
	const (
		slug      = "fake-model"
		table     = "vec_fake_model_8"
		dimension = 8
	)
	_, err = db.Exec(fmt.Sprintf(
		`CREATE VIRTUAL TABLE IF NOT EXISTS %s USING vec0(item_id TEXT PRIMARY KEY, embedding FLOAT[%d] distance_metric=cosine)`,
		table, dimension))
	require.NoError(t, err, "create fake-model vec0 table")
	_, err = db.Exec(`INSERT OR IGNORE INTO embedding_model
		(slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
		VALUES (?, ?, ?, ?, ?, 0, 'active', '{}', strftime('%s','now'))`,
		slug, "Fake 8-dim", "fake", dimension, table)
	require.NoError(t, err, "seed fake-model embedding_model row")

	// The SQLite VectorStore.Search JOINs context_item for scope/kind
	// filtering, so the repo must be backed by the SAME db. The in-memory
	// fakeRepo won't do — its items are invisible to SQLite.
	return sqlite.NewVectorStore(db), sqlite.NewContextRepo(db), db
}
