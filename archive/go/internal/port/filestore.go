package port

// FileStore holds large content blobs (>4KB) on disk, addressed by sha256 hash.
type FileStore interface {
    // Put writes content and returns a content_uri ("file://<relative-path>")
    // and the sha256 hash. If content already exists, returns existing URI.
    Put(content []byte, mime string) (uri string, hash string, err error)
    // Get retrieves content by uri.
    Get(uri string) ([]byte, error)
    // Delete decrements refcount; file removed only when refcount hits 0.
    Delete(uri string) error
}
