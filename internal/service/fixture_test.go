package service

import (
	"path/filepath"
	"testing"

	"uni-context/internal/adapter/fsstore"
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
