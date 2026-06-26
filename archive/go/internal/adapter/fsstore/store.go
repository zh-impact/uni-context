package fsstore

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

type FileStore struct {
	root string
	mu   sync.Mutex
}

func New(root string) (*FileStore, error) {
	if err := os.MkdirAll(root, 0o755); err != nil {
		return nil, fmt.Errorf("create filestore root: %w", err)
	}
	return &FileStore{root: root}, nil
}

func (s *FileStore) Put(content []byte, mime string) (uri string, hash string, err error) {
	sum := sha256.Sum256(content)
	hex := hex.EncodeToString(sum[:])
	hash = "sha256:" + hex
	bucket := hex[:2]
	dir := filepath.Join(s.root, bucket)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", "", fmt.Errorf("mkdir bucket: %w", err)
	}
	contentPath := filepath.Join(dir, hex)
	metaPath := contentPath + ".meta"

	s.mu.Lock()
	defer s.mu.Unlock()

	// Idempotent: if file exists, just bump refcount.
	if _, err := os.Stat(contentPath); err == nil {
		if err := s.bumpRefcount(metaPath, +1); err != nil {
			return "", "", err
		}
		return s.uriFor(hex), hash, nil
	}

	if err := os.WriteFile(contentPath, content, 0o644); err != nil {
		return "", "", fmt.Errorf("write content: %w", err)
	}
	if err := s.writeMeta(metaPath, 1, mime, len(content)); err != nil {
		_ = os.Remove(contentPath)
		return "", "", err
	}
	return s.uriFor(hex), hash, nil
}

func (s *FileStore) Get(uri string) ([]byte, error) {
	hex, err := s.hashFromURI(uri)
	if err != nil {
		return nil, err
	}
	path := s.pathFor(hex)
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("content not found: %s", hex)
		}
		return nil, err
	}
	return data, nil
}

func (s *FileStore) Delete(uri string) error {
	hex, err := s.hashFromURI(uri)
	if err != nil {
		return err
	}
	contentPath := s.pathFor(hex)
	metaPath := contentPath + ".meta"

	s.mu.Lock()
	defer s.mu.Unlock()

	meta, err := s.readMeta(metaPath)
	if err != nil {
		return err
	}
	meta.RefCount--
	if meta.RefCount > 0 {
		return s.writeMeta(metaPath, meta.RefCount, meta.MIME, meta.Size)
	}
	if err := os.Remove(contentPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.Remove(metaPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func (s *FileStore) uriFor(hex string) string {
	return "file://" + hex
}

func (s *FileStore) pathFor(hex string) string {
	return filepath.Join(s.root, hex[:2], hex)
}

func (s *FileStore) hashFromURI(uri string) (string, error) {
	if !strings.HasPrefix(uri, "file://") {
		return "", fmt.Errorf("unsupported uri scheme: %s", uri)
	}
	hex := strings.TrimPrefix(uri, "file://")
	if len(hex) != 64 { // sha256 hex length
		return "", fmt.Errorf("malformed hash in uri: %s", uri)
	}
	return hex, nil
}

type meta struct {
	RefCount int    `json:"refcount"`
	MIME     string `json:"mime"`
	Size     int    `json:"size"`
}

func (s *FileStore) readMeta(path string) (meta, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return meta{}, fmt.Errorf("read meta: %w", err)
	}
	var m meta
	if err := json.Unmarshal(data, &m); err != nil {
		return meta{}, fmt.Errorf("unmarshal meta: %w", err)
	}
	return m, nil
}

func (s *FileStore) writeMeta(path string, refcount int, mime string, size int) error {
	m := meta{RefCount: refcount, MIME: mime, Size: size}
	data, _ := json.Marshal(m)
	return os.WriteFile(path, data, 0o644)
}

func (s *FileStore) bumpRefcount(path string, delta int) error {
	m, err := s.readMeta(path)
	if err != nil {
		return err
	}
	m.RefCount += delta
	if m.RefCount < 0 {
		m.RefCount = 0
	}
	return s.writeMeta(path, m.RefCount, m.MIME, m.Size)
}
