package service

import (
	"database/sql"
	"io"
	"path/filepath"
	"testing"

	"uni-context/internal/adapter/fsstore"
	"uni-context/internal/adapter/sqlite"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/require"
)

type searchFixture struct {
	ingest *IngestService
	svc    *SearchService
}

func newSearchFixture(t *testing.T) *searchFixture {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	require.NoError(t, sqlite.Migrate(db))
	t.Cleanup(func() { db.Close() })

	repo := sqlite.NewContextRepo(db)
	searcher := sqlite.NewSearcher(db)
	fs, err := fsstore.New(filepath.Join(t.TempDir(), "fs"))
	require.NoError(t, err)

	return &searchFixture{
		ingest: NewIngestService(repo, fs, io.Discard),
		svc:    NewSearchService(searcher, repo, io.Discard),
	}
}
