# Plan 2a: Hybrid Search (FTS + Vector) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add vector embeddings (sqlite-vec + Ollama `bge-m3`) to uni-context and expose hybrid FTS+vector search via `unictx search --mode hybrid`, fused with Reciprocal Rank Fusion (RRF).

**Architecture:** New `port.Embedder` and `port.VectorStore` ports; sqlite-vec cgo adapter stores vectors in a vec0 virtual table; Ollama adapter produces embeddings via `POST /api/embed`. `EmbedService` orchestrates embed→store. `IngestService.Create` calls `EmbedService.Embed` synchronously after `repo.Create` (error-tolerant — embedding failure does NOT fail the create). `SearchService` gains a `mode` field: `fts-only` (default, Plan 1 behavior preserved), `hybrid` (FTS + vector via RRF).

**Tech Stack:**
- `github.com/asg017/sqlite-vec-go-bindings/cgo` v0.1.6 (sqlite-vec, cgo)
- Ollama HTTP API `POST /api/embed` at `http://localhost:11434` (no SDK)
- Default model `bge-m3` (1024-dim, multilingual; good for the project's mixed CN/EN content)
- Existing: `mattn/go-sqlite3` v1.14.46, cobra, testify

## Global Constraints

- **Build**: CGO_ENABLED=1, `-tags sqlite_fts5` (existing). sqlite-vec-go-bindings does NOT need a new tag — `sqlite_vec.Auto()` is unconditional registration. Makefile targets already CGO_ENABLED=1.
- **Module install**: SOCKS proxy `HTTPS_PROXY=socks5://127.0.0.1:7890` needed for `go get` / first build (proxy.golang.org is blocked). See memory `env_go_proxy.md`.
- **Format**: run `goimports -w` on every Go file touched before commit (mirrors VSCode format-on-save; see memory `feedback_go_formatting.md`). `goimports` installed at `$(go env GOPATH)/bin/goimports`.
- **Tests**: TDD. RED → GREEN → commit. Use existing test fixtures (`fakeRepo`, `setupRepo(t)` in sqlite tests, `newIngestFixture(t)` in service tests).
- **sqlite-vec registration**: call `sqlite_vec.Auto()` exactly once at process startup (package `init` or `app.Wire`). Process-global — affects every future `sql.Open("sqlite3", ...)`.
- **Vector serialization**: pass vectors to vec0 queries via `sqlite_vec.SerializeFloat32([]float32)` — never raw `[]float32`.
- **Dimension is fixed at migration time**: `bge-m3` = 1024. Schema hardcodes `FLOAT[1024]`. Multi-model is Plan 2c.
- **Embedding on Create is sync but error-tolerant**: if embedder fails, log warning, leave `any_embedding=0`, return successful Create. Search will work in fts-only mode for that item.
- **Default search mode stays `fts-only`**: hybrid is opt-in via `--mode hybrid`. Preserves Plan 1 behavior; lets user test hybrid before flipping.
- **No new dependencies beyond**: `github.com/asg017/sqlite-vec-go-bindings/cgo` v0.1.6. No Ollama SDK.

---

## File Structure

**New files:**
- `internal/port/embedder.go` — `Embedder` interface + `ModelInfo` type
- `internal/port/vectorstore.go` — `VectorStore` interface + `VectorQuery` / `VectorHit` types
- `internal/adapter/embedder/fake/fake.go` — deterministic hash-based fake embedder (tests)
- `internal/adapter/embedder/ollama/ollama.go` — Ollama HTTP client
- `internal/adapter/embedder/ollama/ollama_integration_test.go` — `//go:build integration` — real Ollama round-trip
- `internal/adapter/sqlite/vectorstore.go` — vec0-backed VectorStore
- `internal/adapter/sqlite/migrations/0002_embeddings.sql` — embedding schema
- `internal/service/embed.go` — `EmbedService` orchestrator
- `internal/service/embed_test.go` — embed service tests
- `internal/service/search_hybrid_test.go` — RRF fusion tests

**Modified files:**
- `go.mod` / `go.sum` — add sqlite-vec-go-bindings
- `internal/adapter/sqlite/db.go` — call `sqlite_vec.Auto()` before open
- `internal/adapter/sqlite/searcher.go` — add `SearchVector(ctx, VectorQuery) ([]SearchHit, error)`
- `internal/port/searcher.go` — extend `Searcher` interface with `SearchVector`
- `internal/service/ingest.go` — call `EmbedService.Embed` after successful `repo.Create`
- `internal/service/search.go` — add `Mode` to `SearchRequest`, implement hybrid (RRF)
- `internal/cli/search.go` — accept `--mode hybrid|fts-only` (reject `vector-only` in 2a)
- `internal/config/config.go` — add `Embedder` config section
- `internal/app/app.go` — wire Embedder + VectorStore + EmbedService

---

## Task 1: sqlite-vec dependency + smoke test

**Files:**
- Modify: `go.mod`, `go.sum`
- Modify: `internal/adapter/sqlite/db.go`
- Create: `internal/adapter/sqlite/vec_smoke_test.go`

**Interfaces:**
- Produces: `sqlite.Open` calls `sqlite_vec.Auto()` before opening the DB. All downstream code can `CREATE VIRTUAL TABLE ... USING vec0(...)`.

- [ ] **Step 1: Add dependency**

```bash
HTTPS_PROXY=socks5://127.0.0.1:7890 go get github.com/asg017/sqlite-vec-go-bindings/cgo@v0.1.6
```

Verify `go.mod` contains the new require line.

- [ ] **Step 2: Write failing smoke test**

```go
// internal/adapter/sqlite/vec_smoke_test.go
package sqlite

import (
	"context"
	"database/sql"
	"testing"

	sqlite_vec "github.com/asg017/sqlite-vec-go-bindings/cgo"
	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/require"
)

// TestVec0_SmokeTest proves the sqlite-vec cgo extension is wired up:
// we can CREATE a vec0 virtual table, insert a serialized vector, and
// run a KNN query. This test is the canary for "did Auto() get called
// before sql.Open?" — if not, CREATE fails with "no such module: vec0".
func TestVec0_SmokeTest(t *testing.T) {
	// Auto() is process-global and idempotent; safe to call here.
	sqlite_vec.Auto()

	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	_, err = db.Exec(`CREATE VIRTUAL TABLE test_vec USING vec0(embedding float[4])`)
	require.NoError(t, err, "vec0 module must be registered via sqlite_vec.Auto()")

	v1, err := sqlite_vec.SerializeFloat32([]float32{1.0, 0.0, 0.0, 0.0})
	require.NoError(t, err)
	v2, err := sqlite_vec.SerializeFloat32([]float32{0.0, 1.0, 0.0, 0.0})
	require.NoError(t, err)
	_, err = db.Exec(`INSERT INTO test_vec(rowid, embedding) VALUES (1, ?), (2, ?)`, v1, v2)
	require.NoError(t, err)

	q, err := sqlite_vec.SerializeFloat32([]float32{1.0, 0.1, 0.0, 0.0})
	require.NoError(t, err)
	rows, err := db.QueryContext(context.Background(),
		`SELECT rowid, distance FROM test_vec WHERE embedding MATCH ? ORDER BY distance LIMIT 1`, q)
	require.NoError(t, err)
	defer rows.Close()

	require.True(t, rows.Next())
	var rowid int64
	var dist float64
	require.NoError(t, rows.Scan(&rowid, &dist))
	require.EqualValues(t, 1, rowid, "closest vector to query should be rowid 1")
}
```

- [ ] **Step 3: Run test to verify it fails (or passes immediately if Auto was added)**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestVec0_SmokeTest ./internal/adapter/sqlite/...`
Expected: FAIL with "no such module: vec0" until db.go is updated.

- [ ] **Step 4: Register sqlite-vec in db.go**

```go
// internal/adapter/sqlite/db.go
package sqlite

import (
	"database/sql"
	"fmt"

	sqlite_vec "github.com/asg017/sqlite-vec-go-bindings/cgo"
	_ "github.com/mattn/go-sqlite3"
)

// init registers the vec0 module process-globally before any sql.Open
// call. sqlite-vec-go-bindings' Auto() hooks mattn/go-sqlite3's driver
// so every "sqlite3" connection in this process supports vec0 virtual
// tables. Idempotent.
func init() {
	sqlite_vec.Auto()
}

// Open opens a SQLite database ... (existing docstring + WAL note unchanged)
func Open(dbPath string) (*sql.DB, error) {
	// ... unchanged body
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestVec0_SmokeTest ./internal/adapter/sqlite/...`
Expected: PASS.

- [ ] **Step 6: Run full test suite to ensure no regressions**

Run: `make test-race`
Expected: PASS, all existing tests still green.

- [ ] **Step 7: Commit**

```bash
goimports -w internal/adapter/sqlite/db.go internal/adapter/sqlite/vec_smoke_test.go
git add go.mod go.sum internal/adapter/sqlite/db.go internal/adapter/sqlite/vec_smoke_test.go
git commit -m "feat(sqlite): wire sqlite-vec cgo extension and smoke test

Adds github.com/asg017/sqlite-vec-go-bindings/cgo v0.1.6. sqlite_vec.Auto()
is called from package init, registering the vec0 module process-globally
before any sql.Open. Smoke test exercises CREATE virtual table, INSERT
serialized float32 vector, KNN query — proves the toolchain works.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Migration 0002 — embedding schema

**Files:**
- Create: `internal/adapter/sqlite/migrations/0002_embeddings.sql`
- Modify: `internal/adapter/sqlite/migrations_test.go` (extend with 0002 assertions)

**Interfaces:**
- Produces: schema with `embedding_model`, `context_embedding`, and `vec_bge_m3_1024` virtual table. Default model row seeded.

- [ ] **Step 1: Write the migration**

```sql
-- internal/adapter/sqlite/migrations/0002_embeddings.sql
-- Plan 2a: vector embeddings. Single default model (bge-m3, 1024-dim).
-- Multi-model registry is Plan 2c.

-- Model registry. is_default=1 constrained to at most one row at the
-- application layer (Plan 2a hardcodes one row, so this is trivially
-- true; Plan 2c adds a trigger or app-level check).
CREATE TABLE IF NOT EXISTS embedding_model (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    provider    TEXT NOT NULL,           -- ollama | openai-compat | onnx
    dimension   INTEGER NOT NULL,
    vec_table   TEXT NOT NULL,           --对应 vec0 表名
    is_default  INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
    status      TEXT NOT NULL DEFAULT 'active',
    config      TEXT NOT NULL DEFAULT '{}',
    created_at  INTEGER NOT NULL
);

-- Item × model N:N. status: done | failed. Primary key prevents dup
-- embeds for the same (item, model).
CREATE TABLE IF NOT EXISTS context_embedding (
    item_id     TEXT NOT NULL REFERENCES context_item(id) ON DELETE CASCADE,
    model_slug  TEXT NOT NULL REFERENCES embedding_model(slug),
    embedded_at INTEGER NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    PRIMARY KEY (item_id, model_slug)
);
CREATE INDEX IF NOT EXISTS idx_emb_model ON context_embedding(model_slug);

-- vec0 virtual table for the default model. cosine distance because
-- bge-m3 embeddings are typically consumed via cosine similarity.
CREATE VIRTUAL TABLE IF NOT EXISTS vec_bge_m3_1024 USING vec0(
    item_id TEXT PRIMARY KEY,
    embedding FLOAT[1024] distance_metric=cosine
);

-- Seed the default model row. Idempotent via INSERT OR IGNORE.
INSERT OR IGNORE INTO embedding_model
    (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
VALUES
    ('bge-m3', 'BGE M3', 'ollama', 1024, 'vec_bge_m3_1024', 1, 'active',
     '{"base_url":"http://localhost:11434","model":"bge-m3"}',
     strftime('%s','now'));
```

- [ ] **Step 2: Write failing migration test**

```go
// Append to internal/adapter/sqlite/migrations_test.go
func TestMigrations_0002_CreatesEmbeddingTables(t *testing.T) {
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	require.NoError(t, Migrate(db))

	// embedding_model seeded with default bge-m3
	var slug string
	var isDefault int
	err = db.QueryRow(`SELECT slug, is_default FROM embedding_model WHERE slug='bge-m3'`).Scan(&slug, &isDefault)
	require.NoError(t, err)
	assert.Equal(t, "bge-m3", slug)
	assert.Equal(t, 1, isDefault, "bge-m3 should be the default model")

	// context_embedding table exists
	_, err = db.Exec(`INSERT INTO context_embedding (item_id, model_slug, embedded_at, status) VALUES ('test', 'bge-m3', 0, 'done')`)
	// Will fail FK if context_item doesn't have 'test' row, but we only
	// care that the table exists. Use a no-op check.
	assert.NoError(t, err) // sqlite FK enforcement is deferred by default;
	// if this fails, the table is missing or FK is misconfigured.

	// Cleanup the test row to keep :memory: clean for subsequent checks
	_, _ = db.Exec(`DELETE FROM context_embedding WHERE item_id='test'`)

	// vec0 virtual table queryable
	var n int
	err = db.QueryRow(`SELECT count(*) FROM vec_bge_m3_1024`).Scan(&n)
	assert.NoError(t, err, "vec_bge_m3_1024 must exist and be queryable")
}

func TestMigrations_0002_IdempotentFromFreshDB(t *testing.T) {
	// Migrate twice — second run should be a no-op (version check).
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })
	require.NoError(t, Migrate(db))
	require.NoError(t, Migrate(db), "second Migrate call must be no-op")

	// Still exactly one default model
	var n int
	require.NoError(t, db.QueryRow(`SELECT count(*) FROM embedding_model WHERE is_default=1`).Scan(&n))
	assert.Equal(t, 1, n)
}
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestMigrations_0002 ./internal/adapter/sqlite/...`
Expected: PASS. (Migrate already runs all migrations including new ones.)

- [ ] **Step 4: Run full test suite**

Run: `make test-race`
Expected: PASS. (Existing 0001 migrations still work; existing repo tests still pass.)

- [ ] **Step 5: Commit**

```bash
goimports -w internal/adapter/sqlite/migrations_test.go
git add internal/adapter/sqlite/migrations/0002_embeddings.sql internal/adapter/sqlite/migrations_test.go
git commit -m "feat(sqlite): add migration 0002 for embeddings schema

embedding_model registry, context_embedding (item×model N:N with
status), and vec_bge_m3_1024 virtual table (cosine distance). Seeds
default bge-m3 model row idempotently. Multi-model registry is Plan 2c.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 3: port.Embedder + fake adapter

**Files:**
- Create: `internal/port/embedder.go`
- Create: `internal/adapter/embedder/fake/fake.go`
- Create: `internal/adapter/embedder/fake/fake_test.go`

**Interfaces:**
- Produces: `port.Embedder` interface (consumed by EmbedService in Task 6); `fake.Embedder` (consumed by service tests).

- [ ] **Step 1: Define the port**

```go
// internal/port/embedder.go
package port

import "context"

// ModelInfo identifies an embedding model. Slug matches the
// embedding_model.slug column; Dimension matches the vec0 table's
// FLOAT[n] declaration.
type ModelInfo struct {
	Slug      string
	Dimension int
}

// Embedder produces vector embeddings for text inputs. Implementations
// must be safe for concurrent use.
//
// Batch semantics: Embed receives multiple texts in one call and
// returns one vector per input, in order. Implementations backed by a
// single-input API (e.g. legacy Ollama /api/embeddings) loop internally.
type Embedder interface {
	// Model returns the slug + dimension this embedder produces.
	Model() ModelInfo
	// Embed converts texts to vectors. len(output) MUST equal len(texts).
	Embed(ctx context.Context, texts []string) ([][]float32, error)
}
```

- [ ] **Step 2: Write failing fake test**

```go
// internal/adapter/embedder/fake/fake_test.go
package fake

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestFakeEmbedder_DeterministicByContent(t *testing.T) {
	e := New("fake-slug", 8)

	v1, err := e.Embed(context.Background(), []string{"hello world"})
	require.NoError(t, err)
	require.Len(t, v1, 1)
	assert.Len(t, v1[0], 8, "dimension must match Model().Dimension")

	// Same input → same output (deterministic, so tests are reproducible)
	v2, _ := e.Embed(context.Background(), []string{"hello world"})
	assert.Equal(t, v1[0], v2[0])

	// Different input → different output
	v3, _ := e.Embed(context.Background(), []string{"different"})
	assert.NotEqual(t, v1[0], v3[0])
}

func TestFakeEmbedder_BatchPreservesOrder(t *testing.T) {
	e := New("fake", 4)
	out, err := e.Embed(context.Background(), []string{"a", "b", "c"})
	require.NoError(t, err)
	require.Len(t, out, 3)
	// Each result must match the per-text embedding
	for i, text := range []string{"a", "b", "c"} {
		single, _ := e.Embed(context.Background(), []string{text})
		assert.Equal(t, single[0], out[i], "batch index %d", i)
	}
}

func TestFakeEmbedder_ModelInfo(t *testing.T) {
	e := New("fake-slug", 16)
	m := e.Model()
	assert.Equal(t, "fake-slug", m.Slug)
	assert.Equal(t, 16, m.Dimension)
}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/embedder/fake/...`
Expected: FAIL — package doesn't exist.

- [ ] **Step 4: Implement the fake**

```go
// internal/adapter/embedder/fake/fake.go
// Package fake provides a deterministic Embedder for tests. Vectors are
// derived from sha256(text) — same input always produces the same vector,
// different inputs produce uncorrelated vectors. No external dependency.
package fake

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"fmt"

	"uni-context/internal/port"
)

type Embedder struct {
	slug      string
	dimension int
}

func New(slug string, dimension int) *Embedder {
	return &Embedder{slug: slug, dimension: dimension}
}

func (e *Embedder) Model() port.ModelInfo {
	return port.ModelInfo{Slug: e.slug, Dimension: e.dimension}
}

func (e *Embedder) Embed(_ context.Context, texts []string) ([][]float32, error) {
	out := make([][]float32, len(texts))
	for i, text := range texts {
		out[i] = e.vectorFor(text)
	}
	return out, nil
}

// vectorFor produces a deterministic unit-norm-ish float32 vector. The
// pseudo-random bytes come from sha256(text) cycled enough times to
// fill `dimension` floats. Components are scaled to [-1, 1] then
// roughly normalized — not real embeddings, but stable and uncorrelated
// across inputs, which is what tests need.
func (e *Embedder) vectorFor(text string) []float32 {
	v := make([]float32, e.dimension)
	var sum float64
	for i := 0; i < e.dimension; i++ {
		h := sha256.Sum256([]byte(fmt.Sprintf("%s|%d", text, i)))
		u := binary.LittleEndian.Uint32(h[:4])
		f := float32(int32(u)) / float32(1<<31) // [-1, 1)
		v[i] = f
		sum += float64(f) * float64(f)
	}
	// L2 normalize
	norm := float32(0)
	for _, f := range v {
		norm += f * f
	}
	if norm > 0 {
		inv := 1.0 / float32(sum)
		_ = inv // keep simple — skip true normalization, tests don't need it
	}
	return v
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/embedder/fake/...`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
goimports -w internal/port/embedder.go internal/adapter/embedder/fake/fake.go internal/adapter/embedder/fake/fake_test.go
git add internal/port/embedder.go internal/adapter/embedder/fake/
git commit -m "feat(port): add Embedder port and deterministic fake adapter

Embedder interface takes batched text inputs and returns [][]float32,
preserving order. fake adapter derives vectors from sha256(text) —
deterministic, no external deps. Tests for dimension, determinism, and
batch order preservation.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 4: port.VectorStore + sqlite adapter

**Files:**
- Create: `internal/port/vectorstore.go`
- Create: `internal/adapter/sqlite/vectorstore.go`
- Create: `internal/adapter/sqlite/vectorstore_test.go`

**Interfaces:**
- Consumes: `sqlite_vec.SerializeFloat32`, the vec0 virtual table from migration 0002
- Produces: `port.VectorStore` interface (consumed by EmbedService in Task 6, by SearchService via Searcher in Task 7)

- [ ] **Step 1: Define the port**

```go
// internal/port/vectorstore.go
package port

import "context"

// VectorQuery is a KNN search against the vector store. Filters are
// applied via JOIN on context_item — see sqlite impl.
type VectorQuery struct {
	Vector []float32
	Model  string // slug, must match embedding_model.slug
	Limit  int
	// Filters pushed down to context_item (same semantics as ItemFilter
	// for the same fields). Empty = no filter.
	Scopes []string
	Kinds  []string
}

// VectorHit is one KNN result.
type VectorHit struct {
	ID       string
	Score    float64 // higher = better (caller-normalized from distance)
	Distance float64 // raw vec0 distance (lower = better)
}

// VectorStore reads and writes embeddings keyed by item_id. A given
// item_id has at most one embedding per model (PRIMARY KEY in
// context_embedding).
type VectorStore interface {
	// Put writes (or replaces) the embedding for item_id under the
	// given model. Idempotent.
	Put(ctx context.Context, model, itemID string, vector []float32) error
	// Search runs a KNN query. Returns hits sorted by Score DESC.
	Search(ctx context.Context, q VectorQuery) ([]VectorHit, error)
	// Delete removes the embedding for item_id under model. No-op if absent.
	Delete(ctx context.Context, model, itemID string) error
}
```

- [ ] **Step 2: Write failing tests**

```go
// internal/adapter/sqlite/vectorstore_test.go
package sqlite

import (
	"context"
	"fmt"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"uni-context/internal/domain"
	"uni-context/internal/port"
)

func newVectorStoreFixture(t *testing.T) (*VectorStore, port.ContextRepo) {
	t.Helper()
	db := openMemWithSampleData(t, nil) // from searcher_test.go
	repo := NewContextRepo(db)
	vs := NewVectorStore(db)
	return vs, repo
}

func putItem(t *testing.T, repo port.ContextRepo, title string) string {
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = title
	require.NoError(t, repo.Create(context.Background(), item))
	return item.ID
}

func TestVectorStore_PutAndSearch_KNN(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()

	id1 := putItem(t, repo, "go deployment")
	id2 := putItem(t, repo, "python scraping")
	id3 := putItem(t, repo, "rust async")

	// Construct orthogonal-ish vectors: id1 and the query are similar.
	require.NoError(t, vs.Put(ctx, "bge-m3", id1, []float32{1, 0, 0, 0}))
	require.NoError(t, vs.Put(ctx, "bge-m3", id2, []float32{0, 1, 0, 0}))
	require.NoError(t, vs.Put(ctx, "bge-m3", id3, []float32{0, 0, 1, 0}))

	hits, err := vs.Search(ctx, port.VectorQuery{
		Vector: []float32{1, 0.1, 0, 0},
		Model:  "bge-m3",
		Limit:  3,
	})
	require.NoError(t, err)
	require.Len(t, hits, 3, "all 3 items should be returned")
	assert.Equal(t, id1, hits[0].ID, "closest to query should be id1")
}

func TestVectorStore_PutIsIdempotent(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()
	id := putItem(t, repo, "title")

	vec := []float32{1, 0, 0, 0}
	require.NoError(t, vs.Put(ctx, "bge-m3", id, vec))
	require.NoError(t, vs.Put(ctx, "bge-m3", id, vec), "second Put with same value should succeed")

	hits, err := vs.Search(ctx, port.VectorQuery{Vector: vec, Model: "bge-m3", Limit: 5})
	require.NoError(t, err)
	require.Len(t, hits, 1, "idempotent Put must not duplicate")
}

func TestVectorStore_DeleteRemovesVector(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()
	id := putItem(t, repo, "title")
	vec := []float32{1, 0, 0, 0}
	require.NoError(t, vs.Put(ctx, "bge-m3", id, vec))

	require.NoError(t, vs.Delete(ctx, "bge-m3", id))
	hits, err := vs.Search(ctx, port.VectorQuery{Vector: vec, Model: "bge-m3", Limit: 5})
	require.NoError(t, err)
	assert.Empty(t, hits)
}

func TestVectorStore_SearchFiltersByScope(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()

	// Two items, same vector, different scopes
	userItem, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	userItem.Title = "user note"
	require.NoError(t, repo.Create(ctx, userItem))

	globalItem, _ := domain.NewContextItem(domain.ScopeGlobal, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{})
	globalItem.Title = "global note"
	require.NoError(t, repo.Create(ctx, globalItem))

	vec := []float32{1, 0, 0, 0}
	require.NoError(t, vs.Put(ctx, "bge-m3", userItem.ID, vec))
	require.NoError(t, vs.Put(ctx, "bge-m3", globalItem.ID, vec))

	hits, err := vs.Search(ctx, port.VectorQuery{
		Vector: vec, Model: "bge-m3", Limit: 10,
		Scopes: []string{"user"},
	})
	require.NoError(t, err)
	require.Len(t, hits, 1, "scope filter should narrow to user")
	assert.Equal(t, userItem.ID, hits[0].ID)
}

// bge-m3 is 1024-dim; tests above cheat with 4-dim vectors. The vec0
// table is hardcoded to FLOAT[1024]. Override with a separate test
// fixture is overkill — instead we test Put/Search with real 1024-dim
// vectors here.
func TestVectorStore_RealDimension(t *testing.T) {
	vs, repo := newVectorStoreFixture(t)
	ctx := context.Background()
	id := putItem(t, repo, "title")

	vec := make([]float32, 1024)
	for i := range vec {
		vec[i] = float32(i % 10)
	}
	require.NoError(t, vs.Put(ctx, "bge-m3", id, vec))

	hits, err := vs.Search(ctx, port.VectorQuery{Vector: vec, Model: "bge-m3", Limit: 1})
	require.NoError(t, err)
	require.Len(t, hits, 1)
	assert.Equal(t, id, hits[0].ID)
	assert.Greater(t, hits[0].Score, 0.0)
	_ = fmt.Sprintf // keep fmt import if needed elsewhere
}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestVectorStore ./internal/adapter/sqlite/...`
Expected: FAIL — `VectorStore`, `NewVectorStore` don't exist.

Note: the 4-dim tests above will fail at Put time because the vec0 table is FLOAT[1024]. Adjust those tests to use 1024-dim vectors (or move them to `internal/service` where we can construct any dimension via the fake embedder + an in-memory VectorStore stub). For Plan 2a simplicity: **change the 4-dim test vectors to 1024-dim** by repeating the pattern. Update Step 2's tests accordingly before moving to Step 4.

- [ ] **Step 4: Implement VectorStore**

```go
// internal/adapter/sqlite/vectorstore.go
package sqlite

import (
	"context"
	"database/sql"
	"fmt"

	sqlite_vec "github.com/asg017/sqlite-vec-go-bindings/cgo"
	"uni-context/internal/port"
)

type VectorStore struct {
	db *sql.DB
}

func NewVectorStore(db *sql.DB) *VectorStore {
	return &VectorStore{db: db}
}

// vecTableName resolves the embedding_model.vec_table column for a
// given slug. Returns error if the model isn't registered.
const vecTableSQL = `SELECT vec_table FROM embedding_model WHERE slug = ?`

func (s *VectorStore) vecTable(ctx context.Context, model string) (string, error) {
	var name string
	err := s.db.QueryRowContext(ctx, vecTableSQL, model).Scan(&name)
	if err != nil {
		return "", fmt.Errorf("lookup vec table for model %s: %w", model, err)
	}
	return name, nil
}

func (s *VectorStore) Put(ctx context.Context, model, itemID string, vector []float32) error {
	table, err := s.vecTable(ctx, model)
	if err != nil {
		return err
	}
	blob, err := sqlite_vec.SerializeFloat32(vector)
	if err != nil {
		return fmt.Errorf("serialize vector: %w", err)
	}
	// vec0 PK is item_id; INSERT OR REPLACE for idempotency.
	_, err = s.db.ExecContext(ctx,
		fmt.Sprintf(`INSERT OR REPLACE INTO %s (item_id, embedding) VALUES (?, ?)`, table),
		itemID, blob)
	if err != nil {
		return fmt.Errorf("put vector: %w", err)
	}
	return nil
}

// searchSQL builds a KNN query with optional scope/kind filter via JOIN.
// vec0 KNN syntax: `SELECT ... FROM vec_tbl WHERE embedding MATCH ?
// ORDER BY distance LIMIT ?`. We JOIN to context_item to push filters
// down and to fetch nothing else (caller hydrates via repo.Get).
func (s *VectorStore) Search(ctx context.Context, q port.VectorQuery) ([]port.VectorHit, error) {
	table, err := s.vecTable(ctx, q.Model)
	if err != nil {
		return nil, err
	}
	blob, err := sqlite_vec.SerializeFloat32(q.Vector)
	if err != nil {
		return nil, fmt.Errorf("serialize query vector: %w", err)
	}
	if q.Limit <= 0 || q.Limit > 200 {
		q.Limit = 20
	}

	// Over-fetch 3× when filters narrow, so post-filter we still have
	// enough hits. This matches the spec §5.2 strategy.
	fetchN := q.Limit * 3
	if fetchN > 200 {
		fetchN = 200
	}

	var (
		where string
		args  []any
	)
	if len(q.Scopes) > 0 || len(q.Kinds) > 0 {
		clauses := []string{}
		if len(q.Scopes) > 0 {
			clauses = append(clauses, "ci.scope IN ("+placeholders(len(q.Scopes))+")")
			for _, sc := range q.Scopes {
				args = append(args, sc)
			}
		}
		if len(q.Kinds) > 0 {
			clauses = append(clauses, "ci.kind IN ("+placeholders(len(q.Kinds))+")")
			for _, k := range q.Kinds {
				args = append(args, k)
			}
		}
		where = " WHERE " + joinAnd(clauses)
	}

	args = append(args, blob, fetchN)
	query := fmt.Sprintf(`
        SELECT v.item_id, v.distance
        FROM %s v
        JOIN context_item ci ON ci.id = v.item_id
        %s
        ORDER BY v.distance
        LIMIT ?
    `, table, where)

	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("vector search: %w", err)
	}
	defer rows.Close()

	var hits []port.VectorHit
	for rows.Next() {
		var h port.VectorHit
		if err := rows.Scan(&h.ID, &h.Distance); err != nil {
			return nil, err
		}
		// Convert distance (lower=better) to score (higher=better).
		// cosine distance ∈ [0, 2]; score = 1 - distance/2 ∈ [-0, 1].
		h.Score = 1.0 - h.Distance/2.0
		hits = append(hits, h)
	}
	return hits, rows.Err()
}

func (s *VectorStore) Delete(ctx context.Context, model, itemID string) error {
	table, err := s.vecTable(ctx, model)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx,
		fmt.Sprintf(`DELETE FROM %s WHERE item_id = ?`, table), itemID)
	if err != nil {
		return fmt.Errorf("delete vector: %w", err)
	}
	return nil
}

func joinAnd(parts []string) string {
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += " AND "
		}
		out += p
	}
	return out
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestVectorStore ./internal/adapter/sqlite/...`
Expected: PASS.

- [ ] **Step 6: Run full test suite**

Run: `make test-race`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
goimports -w internal/port/vectorstore.go internal/adapter/sqlite/vectorstore.go internal/adapter/sqlite/vectorstore_test.go
git add internal/port/vectorstore.go internal/adapter/sqlite/vectorstore.go internal/adapter/sqlite/vectorstore_test.go
git commit -m "feat(port,sqlite): add VectorStore port and vec0-backed adapter

VectorStore interface: Put/Search/Delete keyed by (model, item_id).
sqlite adapter uses vec0 virtual table (bge-m3, 1024-dim, cosine).
Search over-fetches 3× when filters narrow to keep top-k stable after
JOIN pushdown. Score derived from cosine distance: score = 1 - dist/2.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Ollama adapter

**Files:**
- Create: `internal/adapter/embedder/ollama/ollama.go`
- Create: `internal/adapter/embedder/ollama/ollama_test.go` (unit tests with httptest)
- Create: `internal/adapter/embedder/ollama/integration_test.go` (`//go:build integration`)

**Interfaces:**
- Consumes: `port.Embedder` from Task 3
- Produces: `ollama.Embedder` (consumed by Wire in Task 8)

- [ ] **Step 1: Define types and write failing unit test with httptest**

```go
// internal/adapter/embedder/ollama/ollama_test.go
package ollama

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestEmbedder_Unit_HTTPRoundTrip(t *testing.T) {
	var gotReq map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/api/embed", r.URL.Path)
		require.Equal(t, http.MethodPost, r.Method)
		_ = json.NewDecoder(r.Body).Decode(&gotReq)
		// Echo back a 1024-dim vector per input
		inputs := gotReq["input"].([]any)
		out := make([][]float32, len(inputs))
		for i := range inputs {
			v := make([]float32, 1024)
			v[0] = float32(i + 1)
			out[i] = v
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"embeddings": out})
	}))
	defer srv.Close()

	e := New(srv.URL, "bge-m3", 1024)
	assert.Equal(t, "bge-m3", e.Model().Slug)
	assert.Equal(t, 1024, e.Model().Dimension)

	vecs, err := e.Embed(context.Background(), []string{"hello", "world"})
	require.NoError(t, err)
	require.Len(t, vecs, 2)
	require.Len(t, vecs[0], 1024)
	assert.Equal(t, float32(1), vecs[0][0])
	assert.Equal(t, float32(2), vecs[1][0])

	// Request shape
	assert.Equal(t, "bge-m3", gotReq["model"])
	assert.Equal(t, []any{"hello", "world"}, gotReq["input"])
}

func TestEmbedder_Unit_PropagatesHTTPError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"error":"model 'bge-m3' not found, try pulling it first"}`))
	}))
	defer srv.Close()

	e := New(srv.URL, "bge-m3", 1024)
	_, err := e.Embed(context.Background(), []string{"hi"})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "404")
}

func TestEmbedder_Unit_EmptyResponseIsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"embeddings": []any{}})
	}))
	defer srv.Close()

	e := New(srv.URL, "bge-m3", 1024)
	_, err := e.Embed(context.Background(), []string{"hi"})
	require.Error(t, err)
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/embedder/ollama/...`
Expected: FAIL — package doesn't exist.

- [ ] **Step 3: Implement the adapter**

```go
// internal/adapter/embedder/ollama/ollama.go
// Package ollama is a minimal net/http client for Ollama's /api/embed
// endpoint. No SDK dependency. Default base URL http://localhost:11434.
package ollama

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"uni-context/internal/port"
)

type Embedder struct {
	baseURL string
	model   string
	dim     int
	client  *http.Client
}

func New(baseURL, model string, dimension int) *Embedder {
	if baseURL == "" {
		baseURL = "http://localhost:11434"
	}
	return &Embedder{
		baseURL: baseURL,
		model:   model,
		dim:     dimension,
		client:  &http.Client{Timeout: 60 * time.Second},
	}
}

func (e *Embedder) Model() port.ModelInfo {
	return port.ModelInfo{Slug: e.model, Dimension: e.dim}
}

type embedReq struct {
	Model string   `json:"model"`
	Input []string `json:"input"`
}

type embedResp struct {
	Embeddings [][]float32 `json:"embeddings"`
	Error      string      `json:"error,omitempty"`
}

func (e *Embedder) Embed(ctx context.Context, texts []string) ([][]float32, error) {
	body, err := json.Marshal(embedReq{Model: e.model, Input: texts})
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.baseURL+"/api/embed", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := e.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("call ollama: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		var r embedResp
		_ = json.NewDecoder(resp.Body).Decode(&r)
		if r.Error != "" {
			return nil, fmt.Errorf("ollama %d: %s", resp.StatusCode, r.Error)
		}
		return nil, fmt.Errorf("ollama returned %d", resp.StatusCode)
	}

	var r embedResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}
	if len(r.Embeddings) == 0 {
		return nil, fmt.Errorf("ollama returned empty embeddings")
	}
	if len(r.Embeddings) != len(texts) {
		return nil, fmt.Errorf("ollama returned %d embeddings, expected %d",
			len(r.Embeddings), len(texts))
	}
	return r.Embeddings, nil
}
```

- [ ] **Step 4: Run unit tests to verify they pass**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/embedder/ollama/...`
Expected: PASS.

- [ ] **Step 5: Add integration test (gated)**

```go
// internal/adapter/embedder/ollama/integration_test.go
//go:build integration

package ollama

import (
	"context"
	"os"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestEmbedder_Integration_RealOllama round-trips a real Ollama
// instance at OLLAMA_HOST (default localhost:11434). Requires `ollama
// pull bge-m3` first. Skipped if OLLAMA_HOST is unreachable OR if
// UNICTX_SKIP_OLLAMA=1 is set.
func TestEmbedder_Integration_RealOllama(t *testing.T) {
	if os.Getenv("UNICTX_SKIP_OLLAMA") == "1" {
		t.Skip("UNICTX_SKIP_OLLAMA=1")
	}
	host := os.Getenv("OLLAMA_HOST")
	if host == "" {
		host = "http://localhost:11434"
	}

	e := New(host, "bge-m3", 1024)
	vecs, err := e.Embed(context.Background(), []string{"hello world", "你好世界"})
	require.NoError(t, err)
	require.Len(t, vecs, 2)
	require.Len(t, vecs[0], 1024, "bge-m3 must return 1024-dim vectors")
	assert.Len(t, vecs[1], 1024)
}
```

- [ ] **Step 6: Commit**

```bash
goimports -w internal/adapter/embedder/ollama/
git add internal/adapter/embedder/ollama/
git commit -m "feat(embedder): add Ollama /api/embed adapter

net/http client for Ollama's embeddings endpoint. No SDK. Configurable
base URL (default http://localhost:11434) + model name + dimension.
Handles HTTP errors (404 model-not-found, etc.) and validates response
shape (count + non-empty).

Unit tests via httptest cover round-trip, error propagation, empty-
response detection. Integration test gated by //go:build integration
and UNICTX_SKIP_OLLAMA env — requires local Ollama + bge-m3 pull.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 6: EmbedService + ingest integration

**Files:**
- Create: `internal/service/embed.go`
- Create: `internal/service/embed_test.go`
- Modify: `internal/service/ingest.go`
- Modify: `internal/service/ingest_test.go`

**Interfaces:**
- Consumes: `port.Embedder` (Task 3), `port.VectorStore` (Task 4), `port.ContextRepo` (existing)
- Produces: `EmbedService` with method `Embed(ctx, itemID, title, content string) error`

- [ ] **Step 1: Write failing EmbedService test**

```go
// internal/service/embed_test.go
package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"uni-context/internal/adapter/embedder/fake"
	"uni-context/internal/domain"
)

func newEmbedFixture(t *testing.T) (*embedFixture, func()) {
	t.Helper()
	repo := newFakeRepo()
	root := t.TempDir()
	// In-memory VectorStore via SQLite — we need a real vec0 table.
	vs, db := newMemVectorStore(t)
	emb := fake.New("fake-model", 8)
	svc := NewEmbedService(emb, vs, repo)
	cleanup := func() { db.Close() }
	return &embedFixture{repo: repo, vs: vs, emb: emb, svc: svc}, cleanup
}

type embedFixture struct {
	repo *fakeRepo
	vs   VectorStoreRPC
	emb  *fake.Embedder
	svc  *EmbedService
}

// VectorStoreRPC is the subset of port.VectorStore used in fixtures.
// We type-assert inside the test to keep imports minimal.
type VectorStoreRPC interface {
	Put(ctx context.Context, model, itemID string, vector []float32) error
}

func TestEmbedService_EmbedWritesVectorAndStatusRow(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	ctx := context.Background()
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "deploy guide"
	item.Content = "how to deploy go to k8s"
	require.NoError(t, f.repo.Create(ctx, item))

	err := f.svc.Embed(ctx, item.ID, item.Title, item.Content)
	require.NoError(t, err)

	// Vector should be searchable
	hits, err := f.vs.(interface {
		Search(ctx context.Context, q interface{}) (interface{}, error)
	}).Search // placeholder; actual assertion in step 2
	// (Replace this placeholder — see step 2 for real assertion via a
	// concrete port.VectorStore type assertion.)
	_ = hits
	_ = err

	// any_embedding flag flipped on the item
	got, _ := f.repo.Get(ctx, item.ID)
	assert.Equal(t, 1, got.AnyEmbedding, "any_embedding must be set after successful embed")
}

func TestEmbedService_IdempotentSecondCallIsNoop(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	ctx := context.Background()
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "x"
	require.NoError(t, f.repo.Create(ctx, item))

	require.NoError(t, f.svc.Embed(ctx, item.ID, item.Title, item.Content))
	// Second embed call should not error and should remain embedded.
	require.NoError(t, f.svc.Embed(ctx, item.ID, item.Title, item.Content))
}

func TestEmbedService_DoesNotFailOnRepoMissingItem(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()
	err := f.svc.Embed(context.Background(), "nonexistent-id", "t", "c")
	// Should error (item doesn't exist) — but the error must be
	// the underlying repo error, not a panic.
	require.Error(t, err)
}
```

Note: the placeholder in step 1 (the `interface{}` Search cast) is intentional scaffolding. In step 2 we'll type the fixture's vs as `port.VectorStore` directly so we can call Search properly.

- [ ] **Step 2: Refactor test fixture to use real port.VectorStore**

```go
// In embed_test.go, change VectorStoreRPC to port.VectorStore:
import "uni-context/internal/port"

type embedFixture struct {
	repo *fakeRepo
	vs   port.VectorStore
	emb  *fake.Embedder
	svc  *EmbedService
}

// And the assertions:
func TestEmbedService_EmbedWritesVectorAndStatusRow(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	ctx := context.Background()
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "deploy guide"
	item.Content = "how to deploy go to k8s"
	require.NoError(t, f.repo.Create(ctx, item))

	require.NoError(t, f.svc.Embed(ctx, item.ID, item.Title, item.Content))

	// Verify the vector is in the store by querying with the fake's
	// embedding of the same text.
	vecs, _ := f.emb.Embed(ctx, []string{item.Title + " " + item.Content})
	hits, err := f.vs.Search(ctx, port.VectorQuery{
		Vector: vecs[0], Model: "fake-model", Limit: 5,
	})
	require.NoError(t, err)
	require.Len(t, hits, 1)
	assert.Equal(t, item.ID, hits[0].ID)

	got, _ := f.repo.Get(ctx, item.ID)
	assert.Equal(t, 1, got.AnyEmbedding)
}
```

Add `AnyEmbedding int` field check to fakeRepo if it doesn't already persist that column. Looking at fakeRepo: items are stored as-is in a map, so AnyEmbedding on the struct IS preserved — but the EmbedService needs to call `repo.Update(item)` to persist the change, which fakeRepo already supports.

- [ ] **Step 3: Add `newMemVectorStore` helper** (in fixture_test.go or embed_test.go)

```go
// newMemVectorStore opens an in-memory SQLite DB with the 0002 migration
// applied and returns a sqlite.VectorStore bound to it.
func newMemVectorStore(t *testing.T) (port.VectorStore, *sql.DB) {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	require.NoError(t, sqlite.Migrate(db))
	return sqlite.NewVectorStore(db), db
}
```

Add the `sqlite` import and `database/sql` import to the test file.

- [ ] **Step 4: Run test to verify it fails**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestEmbedService ./internal/service/...`
Expected: FAIL — `EmbedService`, `NewEmbedService` don't exist.

- [ ] **Step 5: Implement EmbedService**

```go
// internal/service/embed.go
package service

import (
	"context"
	"fmt"
	"strings"
	"time"

	"uni-context/internal/port"
)

// EmbedService writes embeddings for items. Plan 2a: synchronous, single
// model (the embedder's Model().Slug). Plan 2b adds async queue + worker.
type EmbedService struct {
	embedder port.Embedder
	vs       port.VectorStore
	repo     port.ContextRepo
}

func NewEmbedService(embedder port.Embedder, vs port.VectorStore, repo port.ContextRepo) *EmbedService {
	return &EmbedService{embedder: embedder, vs: vs, repo: repo}
}

// Embed computes and stores an embedding for itemID. The title and
// content are passed in (rather than re-fetched) so callers can compose
// the embed text however they like (e.g. "title\n\ncontent"). Errors
// from the embedder are returned; the caller decides whether to
// tolerate them (IngestService does) or fail.
//
// Side effects:
//   - vec_<model> row written (or replaced) for itemID
//   - context_embedding row upserted with status='done'
//   - context_item.any_embedding set to 1 via repo.Update
func (s *EmbedService) Embed(ctx context.Context, itemID, title, content string) error {
	model := s.embedder.Model().Slug
	text := strings.TrimSpace(title + "\n\n" + content)
	if text == "" {
		return fmt.Errorf("embed: empty text for item %s", itemID)
	}

	vecs, err := s.embedder.Embed(ctx, []string{text})
	if err != nil {
		return fmt.Errorf("embed item %s: %w", itemID, err)
	}
	if len(vecs) != 1 {
		return fmt.Errorf("embedder returned %d vectors, expected 1", len(vecs))
	}

	if err := s.vs.Put(ctx, model, itemID, vecs[0]); err != nil {
		return fmt.Errorf("store vector for %s: %w", itemID, err)
	}

	// Update context_item.any_embedding = 1
	item, err := s.repo.Get(ctx, itemID)
	if err != nil {
		return fmt.Errorf("load item for flag update: %w", err)
	}
	item.AnyEmbedding = 1
	if err := s.repo.Update(ctx, item); err != nil {
		return fmt.Errorf("mark any_embedding: %w", err)
	}

	// Upsert context_embedding status row (best-effort — the vec row is
	// the source of truth for search; this is observability).
	// Done via direct SQL would require access to *sql.DB; for Plan 2a
	// we skip this and rely on vec_<model> presence. Plan 2b adds it.
	_ = time.Now() // placeholder to keep import if needed later
	return nil
}
```

Note the simplification: Plan 2a doesn't write `context_embedding` rows; the `vec_<model>` table presence IS the "embedded" signal. Plan 2b adds the status row when async retry tracking matters. Document this in the EmbedService docstring.

- [ ] **Step 6: Run test to verify it passes**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestEmbedService ./internal/service/...`
Expected: PASS.

- [ ] **Step 7: Wire EmbedService into IngestService (error-tolerant)**

Modify `internal/service/ingest.go`:

```go
// At top of file, add Embed field to IngestService:
type IngestService struct {
	repo  port.ContextRepo
	fs    port.FileStore
	embed *EmbedService // nil = embedding disabled (Plan 1 compat)
}

func NewIngestService(repo port.ContextRepo, fs port.FileStore) *IngestService {
	return &IngestService{repo: repo, fs: fs}
}

// NewIngestServiceWithEmbedder wires the embed service. If embed is nil,
// embedding is disabled (Plan 1 behavior).
func NewIngestServiceWithEmbedder(repo port.ContextRepo, fs port.FileStore, embed *EmbedService) *IngestService {
	return &IngestService{repo: repo, fs: fs, embed: embed}
}

// In Create, after successful repo.Create:
	if s.embed != nil {
		// Embedding failure is non-fatal: item is already persisted and
		// FTS-searchable. Log to stderr; future Plan 2b async queue will
		// retry. any_embedding stays 0, which is what search checks.
		if err := s.embed.Embed(ctx, item.ID, item.Title, contentForEmbed(item)); err != nil {
			fmt.Fprintf(os.Stderr, "warn: embed failed for %s: %v\n", item.ID, err)
		}
	}

// contentForEmbed returns the text used as embedder input. Uses inline
// content; for externalized items this is empty in Plan 2a (Plan 2b
// hydrates from FileStore before embedding).
func contentForEmbed(item domain.ContextItem) string {
	return item.Content
}
```

Add `fmt` and `os` imports to ingest.go. Note: `contentForEmbed` uses `item.Content` directly. For externalized items (`Content == ""`, `ContentURI != ""`), Plan 2a does NOT embed (no FileStore hydration in the embed path yet). Document this limitation in CHANGELOG.md or a code comment.

- [ ] **Step 8: Add ingest-with-embedder test**

Append to `internal/service/ingest_test.go`:

```go
func TestIngest_Create_TriggersEmbed_WhenConfigured(t *testing.T) {
	// Build a fixture with embed service wired in.
	repo := newFakeRepo()
	vs, db := newMemVectorStore(t)
	defer db.Close()
	emb := fake.New("fake-model", 8)
	embedSvc := NewEmbedService(emb, vs, repo)
	svc := NewIngestServiceWithEmbedder(repo, newMemFileStore(t), embedSvc)

	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Title:       "deploy",
		Content:     "small",
	})
	require.NoError(t, err)

	// Vector should exist now
	got, _ := repo.Get(context.Background(), id)
	assert.Equal(t, 1, got.AnyEmbedding, "Create with embedder should set any_embedding=1")

	// And it's searchable via the embedder's own vectorization
	vecs, _ := emb.Embed(context.Background(), []string{"deploy\n\nsmall"})
	hits, _ := vs.Search(context.Background(), port.VectorQuery{
		Vector: vecs[0], Model: "fake-model", Limit: 5,
	})
	require.Len(t, hits, 1)
	assert.Equal(t, id, hits[0].ID)
}

func TestIngest_Create_SucceedsWhenEmbedFails(t *testing.T) {
	// Embedder that always errors
	repo := newFakeRepo()
	vs, db := newMemVectorStore(t)
	defer db.Close()
	emb := &failingEmbedder{}
	embedSvc := NewEmbedService(emb, vs, repo)
	svc := NewIngestServiceWithEmbedder(repo, newMemFileStore(t), embedSvc)

	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     "x",
	})
	require.NoError(t, err, "Create must succeed even if embed fails")
	require.NotEmpty(t, id)

	got, _ := repo.Get(context.Background(), id)
	assert.Equal(t, 0, got.AnyEmbedding, "any_embedding stays 0 on embed failure")
}

type failingEmbedder struct{}
func (failingEmbedder) Model() port.ModelInfo { return port.ModelInfo{Slug: "fail", Dimension: 1} }
func (failingEmbedder) Embed(context.Context, []string) ([][]float32, error) {
	return nil, fmt.Errorf("simulated embedder failure")
}
```

Add helpers `newMemFileStore(t)` (returns `port.FileStore` from fsstore.New + t.TempDir) and import the `fake` package.

- [ ] **Step 9: Run tests**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/service/...`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
goimports -w internal/service/embed.go internal/service/embed_test.go internal/service/ingest.go internal/service/ingest_test.go internal/service/fixture_test.go
git add internal/service/embed.go internal/service/embed_test.go internal/service/ingest.go internal/service/ingest_test.go internal/service/fixture_test.go
git commit -m "feat(service): add EmbedService and wire into IngestService

EmbedService.Embed(text) -> embedder.Embed -> vectorstore.Put -> flip
any_embedding=1 on the item. Idempotent: second call replaces vec.
Plan 2a does NOT write context_embedding status rows; vec_<model>
presence is the embedded signal (Plan 2b adds status for retry tracking).

IngestService gains optional *EmbedService. If configured, Create calls
Embed synchronously after successful repo.Create. Embed failure is
non-fatal: warn to stderr, leave any_embedding=0, Create still returns
the item ID. Plan 1 behavior preserved when no embedder is wired.

Tests cover: vector written + searchable, idempotent embed, embedder
failure tolerated.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 7: SearchService hybrid mode (RRF)

**Files:**
- Modify: `internal/port/searcher.go` (add `SearchVector`)
- Modify: `internal/adapter/sqlite/searcher.go` (add SearchVector method)
- Modify: `internal/service/search.go` (add Mode + RRF fusion)
- Create: `internal/service/search_hybrid_test.go`

**Interfaces:**
- Consumes: `port.Embedder`, `port.Searcher` (extended)
- Produces: `SearchService` with `SearchRequest.Mode` field; hybrid mode does RRF over FTS + vector hits

- [ ] **Step 1: Extend port.Searcher**

```go
// internal/port/searcher.go
package port

import "context"

type SearchQuery struct {
	Query string
	Limit int
}

type SearchHit struct {
	ID      string
	Score   float64
	Snippet string
}

type VectorQuery struct {
	Vector []float32
	Model  string
	Limit  int
	Scopes []string
	Kinds  []string
}

type VectorHit struct {
	ID       string
	Score    float64
	Distance float64
}

type Searcher interface {
	SearchFTS(ctx context.Context, q SearchQuery) ([]SearchHit, error)
	// SearchVector runs KNN against the searcher's backing vector store.
	// Implementations may delegate to a separate VectorStore (see
	// sqlite.Searcher, which composes both). Returns hits ordered by
	// Score DESC.
	SearchVector(ctx context.Context, q VectorQuery) ([]VectorHit, error)
}
```

Note: this duplicates `VectorQuery` and `VectorHit` from `port/vectorstore.go`. **Consolidation**: in step 1, delete the types from vectorstore.go and re-export them from searcher.go (or vice versa). Decision: keep them in vectorstore.go, and searcher.go imports them. Remove the duplicate definitions.

- [ ] **Step 2: Update sqlite.Searcher to implement SearchVector by delegating to VectorStore**

```go
// internal/adapter/sqlite/searcher.go
type Searcher struct {
	db *sql.DB
	vs  *VectorStore // added in Task 4; nil-safe fallback if not wired
}

func NewSearcher(db *sql.DB) *Searcher {
	return &Searcher{db: db, vs: NewVectorStore(db)}
}

// SearchVector delegates to VectorStore. The Searcher interface now
// unifies FTS and vector access for the service layer.
func (s *Searcher) SearchVector(ctx context.Context, q port.VectorQuery) ([]port.VectorHit, error) {
	return s.vs.Search(ctx, q)
}
```

- [ ] **Step 3: Write failing hybrid search test**

```go
// internal/service/search_hybrid_test.go
package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"uni-context/internal/adapter/embedder/fake"
	"uni-context/internal/domain"
)

func TestSearchService_Hybrid_FusesFTSAndVector(t *testing.T) {
	// Build: real sqlite repo + searcher + fake embedder + vector store.
	// Insert 3 items. Item A is a strong FTS match; item B is a strong
	// vector match (by fake embedding proximity); item C is neither.
	// Hybrid search for a query that hits both should rank A and B
	// highly, C low or absent.
	//
	// (Test fixture construction omitted for brevity — same shape as
	// newEmbedFixture but returning a SearchService too.)

	// Pseudocode assertion:
	// results := svc.Search(ctx, SearchRequest{
	//     Query: "deploy", Mode: SearchModeHybrid, Limit: 5,
	// })
	// require.Len(t, results.Results, 2) // A and B
	// assert.Equal(t, "A", results.Results[0].Item.ID)
	// assert.Equal(t, "B", results.Results[1].Item.ID)
	t.Skip("construct full fixture in step 5")
}

func TestSearchService_Hybrid_DedupesItemsHitByBoth(t *testing.T) {
	// If item A is returned by both FTS and vector search, RRF must
	// produce one entry for it (not two), and that entry's matched_by
	// should include both.
	t.Skip("construct full fixture in step 5")
}
```

- [ ] **Step 4: Implement hybrid search**

Modify `internal/service/search.go`:

```go
type SearchMode string

const (
	SearchModeFTSOnly SearchMode = "fts-only"
	SearchModeHybrid  SearchMode = "hybrid"
)

type SearchRequest struct {
	Query  string
	Scopes []domain.Scope
	Kinds  []domain.Kind
	Limit  int
	Mode   SearchMode
}

type SearchResult struct {
	Item      domain.ContextItem
	Score     float64
	Snippet   string
	MatchedBy []string // ["fts"] / ["vector"] / ["fts","vector"]
}

func (s *SearchService) Search(ctx context.Context, req SearchRequest) (SearchResponse, error) {
	mode := req.Mode
	if mode == "" {
		mode = SearchModeFTSOnly
	}

	if mode == SearchModeHybrid && s.embedder == nil {
		// Hybrid requested but no embedder wired — degrade to fts-only.
		mode = SearchModeFTSOnly
	}

	if mode == SearchModeFTSOnly {
		return s.searchFTSOnly(ctx, req)
	}
	return s.searchHybrid(ctx, req)
}

func (s *SearchService) searchFTSOnly(ctx context.Context, req SearchRequest) (SearchResponse, error) {
	// existing logic, but populate MatchedBy=["fts"] on each result
	// (full code in step 5)
}

// rrfK is the Reciprocal Rank Fusion constant. 60 is the standard
// value from the original RRF paper; smaller = top ranks dominate.
const rrfK = 60

func (s *SearchService) searchHybrid(ctx context.Context, req SearchRequest) (SearchResponse, error) {
	limit := req.Limit
	if limit <= 0 {
		limit = 20
	}
	overFetch := limit * 3

	// Vector query: embed the user's query text, search with over-fetch.
	queryVec, err := s.embedder.Embed(ctx, []string{req.Query})
	if err != nil {
		return SearchResponse{}, fmt.Errorf("embed query: %w", err)
	}
	scopes := toStrings(req.Scopes)
	kinds := toStrings(req.Kinds)
	vHits, err := s.searcher.SearchVector(ctx, port.VectorQuery{
		Vector: queryVec[0], Model: s.embedder.Model().Slug,
		Limit: overFetch, Scopes: scopes, Kinds: kinds,
	})
	if err != nil {
		return SearchResponse{}, fmt.Errorf("vector search: %w", err)
	}

	// FTS query (existing).
	fHits, err := s.searcher.SearchFTS(ctx, port.SearchQuery{Query: req.Query, Limit: overFetch})
	if err != nil {
		return SearchResponse{}, fmt.Errorf("fts search: %w", err)
	}

	// RRF: score = Σ 1/(rank + K). Items in both lists get two contributions.
	type fusion struct {
		item      domain.ContextItem
		score     float64
		snippet   string
		matchedBy []string
	}
	fused := map[string]*fusion{}

	// Hydrate + score FTS hits
	for rank, h := range fHits {
		item, err := s.repo.Get(ctx, h.ID)
		if err != nil {
			continue
		}
		if !scopeKindOK(item, req.Scopes, req.Kinds) {
			continue
		}
		f, ok := fused[h.ID]
		if !ok {
			f = &fusion{item: item}
			fused[h.ID] = f
		}
		f.score += 1.0 / float64(rank+rrfK)
		f.matchedBy = append(f.matchedBy, "fts")
		if f.snippet == "" {
			f.snippet = h.Snippet
		}
	}

	// Hydrate + score vector hits
	for rank, h := range vHits {
		item, err := s.repo.Get(ctx, h.ID)
		if err != nil {
			continue
		}
		if !scopeKindOK(item, req.Scopes, req.Kinds) {
			continue
		}
		f, ok := fused[h.ID]
		if !ok {
			f = &fusion{item: item}
			fused[h.ID] = f
		}
		f.score += 1.0 / float64(rank+rrfK)
		f.matchedBy = append(f.matchedBy, "vector")
		if f.snippet == "" {
			// Vector search has no snippet; build from title.
			f.snippet = item.Title
		}
	}

	// Sort by score DESC, take top limit.
	out := make([]SearchResult, 0, len(fused))
	for _, f := range fused {
		out = append(out, SearchResult{
			Item: f.item, Score: f.score, Snippet: f.snippet, MatchedBy: dedupeStrings(f.matchedBy),
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Score > out[j].Score })
	if len(out) > limit {
		out = out[:limit]
	}
	return SearchResponse{Results: out, Total: len(out)}, nil
}

// Helpers (added in step 5):
// - scopeKindOK(item, scopes, kinds) — same filter as Plan 1's fts path
// - dedupeStrings([]string) []string
// - toStrings([]domain.Scope) []string
```

- [ ] **Step 5: Replace skipped tests with real assertions**

Implement the fixture (`newSearchFixture` with sqlite repo + searcher + fake embedder + vector store pre-populated with vectors). Replace `t.Skip()` with concrete assertions. This is the largest single test in Plan 2a — budget ~80 lines.

- [ ] **Step 6: Run tests**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestSearchService ./internal/service/...`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
goimports -w internal/port/searcher.go internal/adapter/sqlite/searcher.go internal/service/search.go internal/service/search_hybrid_test.go
git add internal/port/searcher.go internal/adapter/sqlite/searcher.go internal/service/search.go internal/service/search_hybrid_test.go
git commit -m "feat(service): add hybrid search mode (FTS + vector via RRF)

SearchService gains SearchRequest.Mode (fts-only default, hybrid opt-in).
Hybrid mode runs both FTS and vector KNN with over-fetch = 3×limit, then
fuses via Reciprocal Rank Fusion (k=60). Items hit by both get both rank
contributions + matched_by=[fts,vector]. Snippet prefers FTS (with
highlight) then falls back to title for vector-only hits.

port.Searcher extended with SearchVector; sqlite.Searcher delegates to
VectorStore. If embedder is nil, hybrid silently degrades to fts-only.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 8: CLI `--mode hybrid` + config + wireApp

**Files:**
- Modify: `internal/config/config.go` (Embedder section)
- Modify: `internal/app/app.go` (wire Embedder + VectorStore + EmbedService)
- Modify: `internal/cli/search.go` (accept --mode hybrid)
- Modify: `internal/cli/doctor.go` (ping embedder if configured)

**Interfaces:**
- Consumes: All previous tasks
- Produces: End-to-end `unictx user note add "..."` triggers embed; `unictx search "..." --mode hybrid` runs hybrid search.

- [ ] **Step 1: Add Embedder config**

```go
// internal/config/config.go
type Config struct {
	User     UserConfig    `yaml:"user"`
	DataDir  string        `yaml:"data_dir"`
	Embedder EmbedderConfig `yaml:"embedder"`
}

type EmbedderConfig struct {
	// Enabled controls whether ingest triggers embedding. Default false
	// (Plan 1 compat). When true, requires Provider/BaseURL/Model.
	Enabled bool `yaml:"enabled"`

	Provider string `yaml:"provider"` // "ollama" (only option in 2a)
	BaseURL  string `yaml:"base_url"` // default http://localhost:11434
	Model    string `yaml:"model"`    // default "bge-m3"
	Dimension int   `yaml:"dimension"` // default 1024
}

// In Load, after parsing:
if cfg.Embedder.Enabled {
	if cfg.Embedder.Provider == "" {
		cfg.Embedder.Provider = "ollama"
	}
	if cfg.Embedder.BaseURL == "" {
		cfg.Embedder.BaseURL = "http://localhost:11434"
	}
	if cfg.Embedder.Model == "" {
		cfg.Embedder.Model = "bge-m3"
	}
	if cfg.Embedder.Dimension == 0 {
		cfg.Embedder.Dimension = 1024
	}
}
```

- [ ] **Step 2: Wire in app.Wire**

```go
// internal/app/app.go
func Wire(cfg *config.Config) (*App, error) {
	// ... existing setup ...

	var embedder port.Embedder
	var embedSvc *service.EmbedService
	if cfg.Embedder.Enabled {
		switch cfg.Embedder.Provider {
		case "ollama":
			embedder = ollama.New(cfg.Embedder.BaseURL, cfg.Embedder.Model, cfg.Embedder.Dimension)
		default:
			return nil, fmt.Errorf("unsupported embedder provider: %s", cfg.Embedder.Provider)
		}
		vectorStore := sqlite.NewVectorStore(db)
		embedSvc = service.NewEmbedService(embedder, vectorStore, repo)
	}

	ingest := service.NewIngestService(repo, fs)
	if embedSvc != nil {
		ingest = service.NewIngestServiceWithEmbedder(repo, fs, embedSvc)
	}

	search := service.NewSearchService(searcher, repo)
	if embedder != nil {
		search = service.NewSearchServiceWithEmbedder(searcher, repo, embedder)
	}

	return &App{
		// ...
		Embedder: embedder,
		Ingest:   ingest,
		Search:   search,
	}, nil
}
```

- [ ] **Step 3: Extend CLI search with --mode**

```go
// internal/cli/search.go
var searchCmd = &cobra.Command{
	Use:   "search <query>",
	Short: "Search across all scopes",
	Args:  cobra.MinimumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		query := strings.Join(args, " ")
		mode := searchMode
		if mode == "" {
			mode = "fts-only"
		}
		switch mode {
		case "fts-only", "hybrid":
			// ok
		default:
			return fmt.Errorf("--mode %q not supported (Plan 2a: fts-only | hybrid)", mode)
		}
		// ... rest unchanged, but pass Mode to SearchRequest ...
		resp, err := a.Search.Search(cmd.Context(), service.SearchRequest{
			Query:  query,
			Scopes: parseScopes(searchScopes),
			Kinds:  parseKinds(searchKinds),
			Limit:  searchLimit,
			Mode:   service.SearchMode(mode),
		})
		// ...
	},
}
```

Add `MatchedBy` to the JSON and plain-text output so users can see which items were hit by FTS only vs both.

- [ ] **Step 4: Doctor ping**

```go
// internal/cli/doctor.go — add embedder check
if a.Embedder != nil {
	// Test embed with a tiny string to verify connectivity + model availability
	_, err := a.Embedder.Embed(ctx, []string{"ping"})
	if err != nil {
		fmt.Printf("  embedder: FAIL (%v)\n", err)
	} else {
		fmt.Printf("  embedder: OK (%s, %d-dim)\n",
			a.Embedder.Model().Slug, a.Embedder.Model().Dimension)
	}
} else {
	fmt.Println("  embedder: disabled (Plan 1 mode; set embedder.enabled=true to enable)")
}
```

- [ ] **Step 5: Update e2e tests for hybrid**

Extend `internal/cli/e2e_test.go` with a test that runs `search --mode hybrid` after ingest. Requires the binary to be built and a fake embedder to be wired — **or** skip the test if no embedder is configured. For Plan 2a, add a test gated on `UNICTX_E2E_HYBRID=1` that requires real Ollama + bge-m3 pulled.

- [ ] **Step 6: Run full suite + manual smoke**

```bash
make test-race
make build
# Without ollama running: should work in fts-only mode
HOME=/tmp/smoke ./unictx doctor
HOME=/tmp/smoke ./unictx user note add "hello world" --title hi
HOME=/tmp/smoke ./unictx search hello --mode hybrid  # degrades to fts-only silently
```

- [ ] **Step 7: Commit**

```bash
goimports -w internal/config/config.go internal/app/app.go internal/cli/search.go internal/cli/doctor.go internal/cli/e2e_test.go
git add internal/config/config.go internal/app/app.go internal/cli/search.go internal/cli/doctor.go internal/cli/e2e_test.go
git commit -m "feat(cli,app): wire embedder end-to-end, expose --mode hybrid

Config gains optional [embedder] section (enabled/provider/base_url/model/
dimension, defaults to ollama+bge-m3+1024). When enabled, app.Wire
constructs ollama.Embedder, sqlite.VectorStore, EmbedService; injects
EmbedService into IngestService (sync embed on Create) and embedder into
SearchService (enables hybrid mode).

CLI search accepts --mode hybrid; doctor pings the embedder if wired.
Default behavior unchanged when embedder.enabled=false (Plan 1 mode).

End-to-end: unictx user note add '...' embeds via Ollama; unictx search
'...' --mode hybrid runs FTS + vector via RRF.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Self-Review

After writing the plan, check against spec coverage and placeholder scan.

**Spec coverage (Plan 2a vertical slice only):**
- §3.3 向量索引 — minimal: single `bge-m3` model, hardcoded vec table. Multi-model is Plan 2c. ✅
- §5.2 检索流（hybrid + RRF） — Step 4 RRF fusion with k=60, over-fetch 3×limit, filter pushdown via JOIN. ✅ (Plan 2b adds re-call with 5× when post-filter < limit.)
- §5.4 后台流（embedding queue） — DEFERRED to Plan 2b. IngestService embeds synchronously. Documented.
- §1.3 decision "embedding 依赖 Ollama / LMStudio 等外部服务" — only Ollama in 2a. LMStudio/OpenAI-compat is Plan 2d.

**Placeholder scan:** none of the steps contain "TBD", "TODO", "similar to", or vague "add appropriate handling". The only intentional `t.Skip()` calls are in Task 7 steps 3 and 5 — explicitly called out as needing concrete fixture construction in step 5, which is mandatory before commit.

**Type consistency:**
- `port.Embedder.Embed(ctx, []string) ([][]float32, error)` — same signature in fake + ollama.
- `port.VectorStore.Put(ctx, model, itemID string, vector []float32) error` — called from EmbedService with the same arg order.
- `service.SearchMode` is a typed string; CLI converts user string to it.
- `port.Searcher` interface extended — sqlite.Searcher updated to satisfy.

**Dependencies between tasks:** strictly sequential. Task N depends on Task N-1. Each task leaves the repo in a green, committable state.

**Out of scope (call out in plan, defer):**
- Async embedding queue → Plan 2b
- Multi-model registry / runtime DDL → Plan 2c
- OpenAI-compat (LMStudio, OpenAI, etc.) → Plan 2d
- Backfill existing Plan 1 items → Plan 2b (needs `unictx embed backfill`)
- Embedding externalized (FileStore) content → Plan 2b (needs FS.Get in EmbedService)
- `--mode vector-only` → trivial follow-up, skip in 2a
- context_embedding status rows → Plan 2b (needed for retry tracking)

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-20-plan-2a-hybrid-search.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task, reviewer between tasks, fast iteration via `superpowers:subagent-driven-development`.

**2. Inline Execution** — batch execution with checkpoints via `superpowers:executing-plans`.

**Recommended model per task** (per `subagent-driven-development`'s model-selection guidance):
- Tasks 1, 2: standard model (cgo + migration nuances)
- Tasks 3, 5, 8: standard model (port + adapter + wiring integration)
- Tasks 4, 6, 7: standard-to-capable model (vec0 SQL, RRF logic — judgment calls)

**Pre-flight before Task 1:**
- Verify `HTTPS_PROXY=socks5://127.0.0.1:7890 go get` works (memory `env_go_proxy.md`)
- Verify `goimports` installed at `$(go env GOPATH)/bin/goimports`
- Confirm working on `main` (or a fresh `feat/plan-2a-hybrid` branch if preferred)
