package fsstore

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestFileStore_PutAndGet(t *testing.T) {
	root := t.TempDir()
	s, err := New(root)
	require.NoError(t, err)

	content := []byte("hello world this is a test")
	uri, hash, err := s.Put(content, "text/plain")
	require.NoError(t, err)
	assert.True(t, strings.HasPrefix(hash, "sha256:"))
	assert.True(t, strings.HasPrefix(uri, "file://"))

	got, err := s.Get(uri)
	require.NoError(t, err)
	assert.Equal(t, content, got)
}

func TestFileStore_PutDeduplicates(t *testing.T) {
	root := t.TempDir()
	s, _ := New(root)

	content := []byte("same content same hash")
	uri1, hash1, _ := s.Put(content, "text/plain")
	uri2, hash2, _ := s.Put(content, "text/plain")

	assert.Equal(t, uri1, uri2)
	assert.Equal(t, hash1, hash2)

	// File exists exactly once on disk
	files, _ := filepath.Glob(filepath.Join(root, "*", hash1[len("sha256:"):]))
	assert.Len(t, files, 1)
}

func TestFileStore_DeleteRefcount(t *testing.T) {
	root := t.TempDir()
	s, _ := New(root)

	content := []byte("to be deleted")
	uri, _, _ := s.Put(content, "text/plain")
	// First delete on a single-ref content removes the file
	require.NoError(t, s.Delete(uri))
	_, err := s.Get(uri)
	assert.Error(t, err)
}

func TestFileStore_GetMissingReturnsError(t *testing.T) {
	root := t.TempDir()
	s, _ := New(root)
	_, err := s.Get("file://nonexistent")
	assert.Error(t, err)
}
