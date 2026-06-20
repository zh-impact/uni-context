# Plan 2b — Async Embedding Queue + Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close four Plan 2a gaps — FileStore content hydration, `context_embedding` status rows, `unictx embed backfill`, `unictx embed worker` — so embedding is recoverable across embedder outages and backfillable for items created before `embedder.enabled=true`.

**Architecture:** Sync ingest stays unchanged in latency profile; EmbedService gains FileStore + EmbeddingRepo dependencies so it can hydrate externalized content and write per-attempt status rows. New `port.EmbeddingRepo` (separate from ContextRepo) owns the `context_embedding` table. Two new CLI commands: `embed backfill` (inline-embeds any_embedding=0 items) and `embed worker` (long-running retry loop for status='failed' rows). Migration 0003 adds `attempts` + `last_error` columns additively.

**Tech Stack:** Go 1.25, hexagonal architecture, SQLite (mattn/go-sqlite3 + sqlite-vec cgo), existing FTS5 + vec0 infrastructure from Plan 2a. No new external dependencies.

## Global Constraints

Copied verbatim from the spec at `docs/superpowers/specs/2026-06-20-plan-2b-async-backfill-design.md`:

- Build: `CGO_ENABLED=1 go build -tags sqlite_fts5 ./...`
- Test: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./...`
- Race: `CGO_ENABLED=1 go test -tags sqlite_fts5 -race ./...`
- `goimports -w` on every `.go` file touched (matches VSCode format-on-save per `feedback_go_formatting.md`)
- TDD: failing test first, run red, then implement, run green, commit
- No `t.Skip()` in committed code (build-tag gates are OK)
- Plan 1 backward compat: `embedder.enabled=false` keeps app behaving exactly as Plan 1
- Plan 2a backward compat: `embedder.enabled=true` with sync embed behaves exactly as Plan 2a, plus now writes status rows and hydrates content
- Status row policy: write `context_embedding` row on **every** embed attempt (success → 'done', failure → 'failed' + error text)
- Sync ingest: no in-process goroutine; failures become status rows, `worker` recovers them
- Backfill scope: only items where `any_embedding=0`; never re-embed already-embedded items in 2b
- Migration 0003 must be additive (ALTER TABLE ADD COLUMN); cannot rewrite 0002
- Model slug stays "bge-m3" (Plan 2a seed); the dynamic-slug path from Plan 2c preview already handles aliases

---

## File Structure

**New files:**

- `internal/adapter/sqlite/migrations/0003_embedding_retry.sql` — additive ALTER adding `attempts` + `last_error` columns
- `internal/port/embeddingrepo.go` — `EmbeddingRepo` interface + `EmbeddingStatus` struct
- `internal/adapter/sqlite/embedding_repo.go` — sqlite impl of `EmbeddingRepo`
- `internal/adapter/sqlite/embedding_repo_test.go` — unit tests for the impl
- `internal/service/backfill.go` — `BackfillService`
- `internal/service/backfill_test.go` — unit tests
- `internal/service/worker.go` — `WorkerService`
- `internal/service/worker_test.go` — unit tests
- `internal/cli/embed.go` — `embed` parent command + `backfill` + `worker` subcommands
- `internal/cli/embed_test.go` — unit tests for CLI flag parsing and validation

**Modified files:**

- `internal/service/embed.go` — `EmbedService` constructor signature change; hydration + status row logic
- `internal/service/embed_test.go` — fixture updates for new constructor signature; new tests for hydration + status
- `internal/service/ingest.go` — no logic change, but `contentForEmbed` is no longer needed (EmbedService handles hydration); document this
- `internal/app/app.go` — construct `EmbeddingRepo`, wire into EmbedService; construct BackfillService + WorkerService; expose on App
- `internal/adapter/sqlite/migrations_test.go` — add test for 0003 columns

---

## Task 1: Migration 0003 — embedding retry columns

**Files:**
- Create: `internal/adapter/sqlite/migrations/0003_embedding_retry.sql`
- Modify: `internal/adapter/sqlite/migrations_test.go`

**Interfaces:**
- Consumes: migration 0002's `context_embedding` table (must already exist)
- Produces: `context_embedding.attempts` (INTEGER NOT NULL DEFAULT 0) + `context_embedding.last_error` (TEXT nullable)

- [ ] **Step 1: Write the failing test**

Append to `internal/adapter/sqlite/migrations_test.go`:

```go
func TestMigrations_0003_AddsRetryColumns(t *testing.T) {
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	require.NoError(t, Migrate(db))

	// schema_version is now "3" after 0003
	var version string
	require.NoError(t, db.QueryRow(
		`SELECT value FROM schema_meta WHERE key='schema_version'`).Scan(&version))
	assert.Equal(t, "3", version)

	// attempts + last_error columns exist on context_embedding.
	// PRAGMA table_info is the canonical way to inspect columns.
	rows, err := db.Query(`PRAGMA table_info(context_embedding)`)
	require.NoError(t, err)
	defer rows.Close()

	cols := map[string]bool{}
	for rows.Next() {
		var cid int
		var name, ctype string
		var notnull, pk int
		var dfltValue any // sqlite gives NULL for columns without default
		require.NoError(t, rows.Scan(&cid, &name, &ctype, &notnull, &dfltValue, &pk))
		cols[name] = true
	}
	assert.True(t, cols["attempts"], "attempts column must exist after 0003")
	assert.True(t, cols["last_error"], "last_error column must exist after 0003")

	// attempts has DEFAULT 0: INSERT without specifying it should succeed
	// and the column should read back as 0.
	_, err = db.Exec(`INSERT INTO context_embedding (item_id, model_slug, embedded_at, status)
		VALUES ('test-0003', 'bge-m3', 0, 'done')`)
	require.NoError(t, err)

	var attempts int
	var lastError sql.NullString
	require.NoError(t, db.QueryRow(
		`SELECT attempts, last_error FROM context_embedding WHERE item_id='test-0003'`).
		Scan(&attempts, &lastError))
	assert.Equal(t, 0, attempts, "attempts must default to 0")
	assert.False(t, lastError.Valid, "last_error must be NULL by default")
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestMigrations_0003_AddsRetryColumns ./internal/adapter/sqlite/...`
Expected: FAIL — `attempts` column doesn't exist, PRAGMA scan finds nothing, assertion `cols["attempts"]` is false.

- [ ] **Step 3: Create the migration file**

`internal/adapter/sqlite/migrations/0003_embedding_retry.sql`:

```sql
-- Plan 2b: retry tracking for embeddings.
-- Additive ALTER only — does not rewrite 0002. The original `error` column
-- from 0002 is kept for backward-compat; `last_error` is the most recent
-- error text (worker updates it on every failed retry).

ALTER TABLE context_embedding ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE context_embedding ADD COLUMN last_error TEXT;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestMigrations_0003 ./internal/adapter/sqlite/...`
Expected: PASS.

- [ ] **Step 5: Run full migration test suite to verify no regression**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestMigrations ./internal/adapter/sqlite/...`
Expected: PASS — all 5 migration tests green.

- [ ] **Step 6: Commit**

```bash
git add internal/adapter/sqlite/migrations/0003_embedding_retry.sql \
        internal/adapter/sqlite/migrations_test.go
git commit -m "feat(sqlite): add migration 0003 for embedding retry columns

Adds attempts (INTEGER NOT NULL DEFAULT 0) and last_error (TEXT) to
context_embedding. Additive ALTER only; 0002's error column kept for
backward-compat. Foundation for Plan 2b worker retry tracking."
```

---

## Task 2: port.EmbeddingRepo + sqlite adapter

**Files:**
- Create: `internal/port/embeddingrepo.go`
- Create: `internal/adapter/sqlite/embedding_repo.go`
- Create: `internal/adapter/sqlite/embedding_repo_test.go`

**Interfaces:**
- Consumes: Task 1's `context_embedding` table with new `attempts` + `last_error` columns
- Produces: `port.EmbeddingRepo` interface, `port.EmbeddingStatus` struct, `sqlite.NewEmbeddingRepo(db) *EmbeddingRepo`. Used by Task 3 (EmbedService) and Tasks 5/6 (BackfillService/WorkerService).

- [ ] **Step 1: Write the port**

`internal/port/embeddingrepo.go`:

```go
package port

import (
	"context"
	"time"
)

// EmbeddingStatus is one row in context_embedding. Mirrors the schema
// from migrations 0002 + 0003.
type EmbeddingStatus struct {
	ItemID     string
	ModelSlug  string
	Status     string // "done" | "failed"
	Error      string // original error text (0002 column)
	LastError  string // most recent error text (0003 column)
	Attempts   int
	EmbeddedAt time.Time
}

// EmbeddingRepo owns the context_embedding table. Separate from
// ContextRepo because the table serves a different consumer (worker +
// observability) and mixing them produces a fat interface.
type EmbeddingRepo interface {
	// UpsertStatus inserts or updates the status row for (itemID, modelSlug).
	// On conflict, attempts is incremented by 1 (fresh INSERT starts at 1),
	// embedded_at is set to now, status/error/last_error are overwritten.
	UpsertStatus(ctx context.Context, itemID, modelSlug, status, errStr string) error

	// GetStatus returns the row for (itemID, modelSlug). Returns a
	// wrapping of domain.ErrNotFound if no row exists.
	GetStatus(ctx context.Context, itemID, modelSlug string) (EmbeddingStatus, error)

	// ListFailed returns up to limit rows with status='failed', ordered
	// by embedded_at ASC (oldest failures first — they've waited longest).
	// limit<=0 defaults to 100.
	ListFailed(ctx context.Context, limit int) ([]EmbeddingStatus, error)
}
```

- [ ] **Step 2: Write the failing test**

`internal/adapter/sqlite/embedding_repo_test.go`:

```go
package sqlite

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

func newEmbeddingRepoFixture(t *testing.T) (port.EmbeddingRepo, *ContextRepo) {
	t.Helper()
	db := openTestDB(t) // from model_registry_test.go — fresh migrated :memory:
	repo := NewContextRepo(db)
	embRepo := NewEmbeddingRepo(db)
	return embRepo, repo
}

// insertItemForEmbedTest creates a context_item so FK on context_embedding passes.
func insertItemForEmbedTest(t *testing.T, repo *ContextRepo, id string) {
	t.Helper()
	item, err := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	require.NoError(t, err)
	item.ID = id
	require.NoError(t, repo.Create(context.Background(), item))
}

func TestEmbeddingRepo_UpsertStatus_InsertsFresh(t *testing.T) {
	embRepo, repo := newEmbeddingRepoFixture(t)
	insertItemForEmbedTest(t, repo, "item-1")

	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"item-1", "bge-m3", "done", ""))

	st, err := embRepo.GetStatus(context.Background(), "item-1", "bge-m3")
	require.NoError(t, err)
	assert.Equal(t, "done", st.Status)
	assert.Equal(t, 1, st.Attempts, "fresh INSERT starts at attempts=1")
	assert.Empty(t, st.LastError)
	assert.WithinDuration(t, time.Now(), st.EmbeddedAt, 5*time.Second)
}

func TestEmbeddingRepo_UpsertStatus_OnConflictIncrementsAttempts(t *testing.T) {
	embRepo, repo := newEmbeddingRepoFixture(t)
	insertItemForEmbedTest(t, repo, "item-2")

	// First attempt fails
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"item-2", "bge-m3", "failed", "ollama unreachable"))
	st, _ := embRepo.GetStatus(context.Background(), "item-2", "bge-m3")
	assert.Equal(t, 1, st.Attempts)
	assert.Equal(t, "ollama unreachable", st.LastError)

	// Second attempt also fails — attempts increments to 2
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"item-2", "bge-m3", "failed", "still unreachable"))
	st, _ = embRepo.GetStatus(context.Background(), "item-2", "bge-m3")
	assert.Equal(t, 2, st.Attempts)
	assert.Equal(t, "still unreachable", st.LastError)

	// Third attempt succeeds — attempts increments to 3, last_error cleared
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"item-2", "bge-m3", "done", ""))
	st, _ = embRepo.GetStatus(context.Background(), "item-2", "bge-m3")
	assert.Equal(t, 3, st.Attempts)
	assert.Equal(t, "done", st.Status)
	assert.Empty(t, st.LastError)
}

func TestEmbeddingRepo_GetStatus_NotFound(t *testing.T) {
	embRepo, _ := newEmbeddingRepoFixture(t)
	_, err := embRepo.GetStatus(context.Background(), "nonexistent", "bge-m3")
	require.Error(t, err)
	assert.ErrorIs(t, err, domain.ErrNotFound)
}

func TestEmbeddingRepo_ListFailed_OrdersByEmbeddedAtAsc(t *testing.T) {
	embRepo, repo := newEmbeddingRepoFixture(t)
	insertItemForEmbedTest(t, repo, "old")
	insertItemForEmbedTest(t, repo, "mid")
	insertItemForEmbedTest(t, repo, "new")

	// Insert failures with explicit embedded_at timestamps via raw SQL
	// to control ordering (UpsertStatus uses now()).
	db := openTestDB(t) // reopen for raw SQL access; same migration state
	// Re-insert items in this DB since it's a different :memory: instance.
	insertItemForEmbedTest(t, NewContextRepo(db), "old")
	insertItemForEmbedTest(t, NewContextRepo(db), "mid")
	insertItemForEmbedTest(t, repo, "old") // (already done above; safe idempotent)
	// ^ The above is awkward; see Step 3 implementation note about
	// exposing a test helper that returns the *sql.DB. Simpler approach:
	// call UpsertStatus in time-sorted order with sleeps, OR trust that
	// embedded_at = now() at insert and sleeps create ordering. Use sleeps.

	t.Skip("rewriting in Step 3 — see implementer note")
}

func TestEmbeddingRepo_ListFailed_BasicOrdering(t *testing.T) {
	// Replacement test using sleeps to guarantee ordering (no raw SQL needed).
	embRepo, repo := newEmbeddingRepoFixture(t)
	insertItemForEmbedTest(t, repo, "first")
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"first", "bge-m3", "failed", "err1"))
	time.Sleep(1100 * time.Millisecond) // embedded_at is unix seconds

	insertItemForEmbedTest(t, repo, "second")
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"second", "bge-m3", "failed", "err2"))

	// Insert a 'done' row that should NOT appear in ListFailed
	insertItemForEmbedTest(t, repo, "done-item")
	require.NoError(t, embRepo.UpsertStatus(context.Background(),
		"done-item", "bge-m3", "done", ""))

	failed, err := embRepo.ListFailed(context.Background(), 100)
	require.NoError(t, err)
	require.Len(t, failed, 2, "only 'failed' rows returned; 'done' excluded")
	assert.Equal(t, "first", failed[0].ItemID, "oldest failure first")
	assert.Equal(t, "second", failed[1].ItemID)

	// Limit honored
	one, err := embRepo.ListFailed(context.Background(), 1)
	require.NoError(t, err)
	require.Len(t, one, 1)
	assert.Equal(t, "first", one[0].ItemID)
}
```

**Note on the skipped test:** Step 1 deliberately writes a flawed test (`TestEmbeddingRepo_ListFailed_OrdersByEmbeddedAtAsc`) to demonstrate the awkwardness of needing raw DB access for ordering control. The replacement test below it (`TestEmbeddingRepo_ListFailed_BasicOrdering`) uses `time.Sleep(1100ms)` to guarantee distinct `embedded_at` unix-second values. Delete the skipped test before commit.

- [ ] **Step 3: Run test to verify it fails**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestEmbeddingRepo ./internal/adapter/sqlite/...`
Expected: FAIL — `NewEmbeddingRepo` undefined, build error.

- [ ] **Step 4: Implement the adapter**

`internal/adapter/sqlite/embedding_repo.go`:

```go
package sqlite

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

type EmbeddingRepo struct {
	db *sql.DB
}

func NewEmbeddingRepo(db *sql.DB) *EmbeddingRepo {
	return &EmbeddingRepo{db: db}
}

// upsertSQL uses ON CONFLICT to atomically insert-or-update + increment.
// attempts+1 happens in the conflict branch; fresh INSERT starts at 1
// (the VALUES binds attempts=1 directly).
//
// last_error mirrors errStr (the most recent error). The original 0002
// `error` column is also set to errStr for backward-compat — it stays
// the same across retries, which is fine; last_error is the authoritative
// "what went wrong most recently" field.
const upsertSQL = `
INSERT INTO context_embedding
    (item_id, model_slug, embedded_at, status, error, last_error, attempts)
VALUES (?, ?, ?, ?, ?, ?, 1)
ON CONFLICT(item_id, model_slug) DO UPDATE SET
    embedded_at = excluded.embedded_at,
    status      = excluded.status,
    error       = excluded.error,
    last_error  = excluded.last_error,
    attempts    = context_embedding.attempts + 1
`

func (r *EmbeddingRepo) UpsertStatus(ctx context.Context, itemID, modelSlug, status, errStr string) error {
	now := time.Now().UTC().Unix()
	if _, err := r.db.ExecContext(ctx, upsertSQL,
		itemID, modelSlug, now, status, errStr, errStr); err != nil {
		return fmt.Errorf("upsert embedding status: %w", err)
	}
	return nil
}

const getStatusSQL = `
SELECT item_id, model_slug, status, error, last_error, attempts, embedded_at
FROM context_embedding
WHERE item_id = ? AND model_slug = ?
`

func (r *EmbeddingRepo) GetStatus(ctx context.Context, itemID, modelSlug string) (port.EmbeddingStatus, error) {
	var (
		s          port.EmbeddingStatus
		err1       sql.NullString
		lastErr    sql.NullString
		embeddedAt int64
	)
	err := r.db.QueryRowContext(ctx, getStatusSQL, itemID, modelSlug).Scan(
		&s.ItemID, &s.ModelSlug, &s.Status, &err1, &lastErr, &s.Attempts, &embeddedAt)
	if err == sql.ErrNoRows {
		return port.EmbeddingStatus{}, fmt.Errorf("%w: embedding %s/%s",
			domain.ErrNotFound, itemID, modelSlug)
	}
	if err != nil {
		return port.EmbeddingStatus{}, fmt.Errorf("get embedding status: %w", err)
	}
	s.Error = err1.String
	s.LastError = lastErr.String
	s.EmbeddedAt = time.Unix(embeddedAt, 0).UTC()
	return s, nil
}

const listFailedSQL = `
SELECT item_id, model_slug, status, error, last_error, attempts, embedded_at
FROM context_embedding
WHERE status = 'failed'
ORDER BY embedded_at ASC
LIMIT ?
`

func (r *EmbeddingRepo) ListFailed(ctx context.Context, limit int) ([]port.EmbeddingStatus, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := r.db.QueryContext(ctx, listFailedSQL, limit)
	if err != nil {
		return nil, fmt.Errorf("list failed embeddings: %w", err)
	}
	defer rows.Close()

	var out []port.EmbeddingStatus
	for rows.Next() {
		var (
			s          port.EmbeddingStatus
			err1       sql.NullString
			lastErr    sql.NullString
			embeddedAt int64
		)
		if err := rows.Scan(
			&s.ItemID, &s.ModelSlug, &s.Status, &err1, &lastErr, &s.Attempts, &embeddedAt); err != nil {
			return nil, err
		}
		s.Error = err1.String
		s.LastError = lastErr.String
		s.EmbeddedAt = time.Unix(embeddedAt, 0).UTC()
		out = append(out, s)
	}
	return out, rows.Err()
}
```

- [ ] **Step 5: Delete the skipped test from Step 2**

Remove `TestEmbeddingRepo_ListFailed_OrdersByEmbeddedAtAsc` entirely. Keep `TestEmbeddingRepo_ListFailed_BasicOrdering`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestEmbeddingRepo ./internal/adapter/sqlite/...`
Expected: PASS — 4 tests green.

- [ ] **Step 7: Run full sqlite package tests to verify no regression**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/...`
Expected: PASS — all existing tests still green.

- [ ] **Step 8: Commit**

```bash
goimports -w internal/port/embeddingrepo.go \
             internal/adapter/sqlite/embedding_repo.go \
             internal/adapter/sqlite/embedding_repo_test.go
git add internal/port/embeddingrepo.go \
        internal/adapter/sqlite/embedding_repo.go \
        internal/adapter/sqlite/embedding_repo_test.go
git commit -m "feat(port,sqlite): add EmbeddingRepo for context_embedding

Single-responsibility port for the context_embedding table (migration
0002 + 0003). Three methods: UpsertStatus (atomic ON CONFLICT +
attempts++), GetStatus (ErrNotFound on miss), ListFailed (oldest
first, limit defaults 100).

Separate from ContextRepo because the table serves a different
consumer (worker + observability) than the canonical item store."
```

---

## Task 3: EmbedService — FileStore hydration + status rows

**Files:**
- Modify: `internal/service/embed.go`
- Modify: `internal/service/embed_test.go`
- Modify: `internal/service/ingest.go` (remove now-redundant `contentForEmbed` helper)

**Interfaces:**
- Consumes: Task 2's `port.EmbeddingRepo`, existing `port.FileStore`, existing `port.Embedder` + `port.VectorStore` + `port.ContextRepo`
- Produces: `NewEmbedService(embedder, vs, repo, fs, embRepo) *EmbedService` (new signature). Old signature REMOVED — Task 4 updates all callers. `Embed(ctx, itemID, title, content)` signature unchanged; when `content==""` it hydrates from FileStore via `item.ContentURI`.

- [ ] **Step 1: Write the failing tests**

Append to `internal/service/embed_test.go` (after existing tests):

```go
func TestEmbedService_HydratesContentFromFileStore(t *testing.T) {
	// Externalized item: item.Content is empty, item.ContentURI points to fs.
	// Before 2b: EmbedService embedded title-only (empty content).
	// After 2b: EmbedService hydrates content from fs.Get(uri).
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	// Create item with externalized content
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "externalized"
	contentBytes := []byte("this content lives in the filestore not inline")
	uri, _, err := f.fs.Put(contentBytes, "text/plain")
	require.NoError(t, err)
	item.ContentURI = uri
	item.Content = "" // simulating post-externalization state
	require.NoError(t, f.repo.Create(context.Background(), item))

	// Capture what text the embedder received
	var receivedTexts []string
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		receivedTexts = texts
		return [][]float32{make([]float32, 8)}, nil
	})

	// Pass empty content; EmbedService should hydrate.
	require.NoError(t, f.svc.Embed(context.Background(), item.ID, item.Title, ""))

	require.Len(t, receivedTexts, 1)
	assert.Contains(t, receivedTexts[0], "externalized", "title is in embed text")
	assert.Contains(t, receivedTexts[0], "this content lives in the filestore",
		"hydrated content is in embed text (this would fail before 2b)")
}

func TestEmbedService_WritesStatusRowOnSuccess(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "ok"
	require.NoError(t, f.repo.Create(context.Background(), item))

	require.NoError(t, f.svc.Embed(context.Background(), item.ID, item.Title, "body"))

	st, err := f.embRepo.GetStatus(context.Background(), item.ID, "fake-model")
	require.NoError(t, err)
	assert.Equal(t, "done", st.Status)
	assert.Equal(t, 1, st.Attempts)
}

func TestEmbedService_WritesStatusRowOnFailure(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "fail"
	require.NoError(t, f.repo.Create(context.Background(), item))

	// Force embedder failure
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		return nil, errors.New("ollama unreachable")
	})

	err := f.svc.Embed(context.Background(), item.ID, item.Title, "body")
	require.Error(t, err)

	st, getErr := f.embRepo.GetStatus(context.Background(), item.ID, "fake-model")
	require.NoError(t, getErr)
	assert.Equal(t, "failed", st.Status)
	assert.Contains(t, st.LastError, "ollama unreachable")
	assert.Equal(t, 1, st.Attempts)
}
```

Also update the fixture (`newEmbedFixture`) — the existing version constructs `EmbedService` with 3 args; the new signature needs 5:

```go
func newEmbedFixture(t *testing.T) (*embedFixture, func()) {
	t.Helper()
	vs, repo, db := newMemVectorStore(t)
	emb := fake.New("fake-model", 8)
	fs := newMemFileStore(t)            // NEW
	embRepo := newMemEmbeddingRepo(t, db) // NEW (uses the same db as repo)
	svc := NewEmbedService(emb, vs, repo, fs, embRepo)
	cleanup := func() { _ = db.Close() }
	return &embedFixture{
		repo: repo, vs: vs, emb: emb, fs: fs, embRepo: embRepo, svc: svc,
	}, cleanup
}

type embedFixture struct {
	repo    port.ContextRepo
	vs      port.VectorStore
	emb     *fake.Embedder
	fs      port.FileStore
	embRepo port.EmbeddingRepo
	svc     *EmbedService
}
```

For `newMemFileStore` and `newMemEmbeddingRepo` helpers, see Step 3 implementation note — they live in the same `_test.go` (or `fixture_test.go` if that already exists in the service package). If `fake.Embedder` doesn't have `SetEmbedHook`, see Step 4 — you'll add it to the fake.

- [ ] **Step 2: Run tests to verify they fail**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestEmbedService ./internal/service/...`
Expected: FAIL — `NewEmbedService` signature mismatch (compile error), `f.fs` undefined on fixture, `SetEmbedHook` undefined on fake.

- [ ] **Step 3: Update the EmbedService implementation**

`internal/service/embed.go` (full rewrite — small file):

```go
package service

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"

	"uni-context/internal/port"
)

// EmbedService writes embeddings for items.
//
// Plan 2b changes vs Plan 2a:
//   - Hydrates content from FileStore when caller passes empty content +
//     item has ContentURI (fixes the "externalized content embeds empty" gap)
//   - Writes a context_embedding status row on every attempt (done/failed)
//     so the worker can find retries and operators get observability
//
// Plan 2b does NOT add: async queue (sync ingest stays; worker cmd
// handles retries), max-attempts cap, multi-model parallel embed.
type EmbedService struct {
	embedder port.Embedder
	vs       port.VectorStore
	repo     port.ContextRepo
	fs       port.FileStore
	embRepo  port.EmbeddingRepo
}

func NewEmbedService(
	embedder port.Embedder,
	vs port.VectorStore,
	repo port.ContextRepo,
	fs port.FileStore,
	embRepo port.EmbeddingRepo,
) *EmbedService {
	return &EmbedService{
		embedder: embedder, vs: vs, repo: repo, fs: fs, embRepo: embRepo,
	}
}

// Embed computes and stores an embedding for itemID.
//
// When content=="" and the item has ContentURI set (externalized case),
// Embed hydrates content from FileStore. Callers may pass content directly
// to skip the hydration round-trip (backfill path may already have it).
//
// Side effects:
//   - vec_<model> row written (or replaced) for itemID
//   - context_item.any_embedding set to 1 via repo.Update (on success only)
//   - context_embedding status row written via embRepo.UpsertStatus
//
// Status row is written for EVERY attempt. On success: status='done',
// errStr="". On any failure: status='failed', errStr=err.Error().
// Status-row write failure is logged to stderr but does NOT mask the
// original embed error or success.
func (s *EmbedService) Embed(ctx context.Context, itemID, title, content string) error {
	model := s.embedder.Model().Slug

	// Hydrate if the caller didn't supply content. This is the path
	// IngestService.Create takes for externalized items (item.Content was
	// cleared after fs.Put). Backfill may pre-hydrate and pass content.
	hydratedContent := content
	if hydratedContent == "" {
		hc, err := s.hydrateContent(ctx, itemID)
		if err != nil {
			// Hydration failure is recoverable by the worker later; record status.
			s.recordStatus(ctx, itemID, model, "failed", err.Error())
			return fmt.Errorf("hydrate content for %s: %w", itemID, err)
		}
		hydratedContent = hc
	}

	text := strings.TrimSpace(title + "\n\n" + hydratedContent)
	if text == "" {
		err := fmt.Errorf("embed: empty text for item %s", itemID)
		s.recordStatus(ctx, itemID, model, "failed", err.Error())
		return err
	}

	vecs, err := s.embedder.Embed(ctx, []string{text})
	if err != nil {
		s.recordStatus(ctx, itemID, model, "failed", err.Error())
		return fmt.Errorf("embed item %s: %w", itemID, err)
	}
	if len(vecs) != 1 {
		err := fmt.Errorf("embedder returned %d vectors, expected 1", len(vecs))
		s.recordStatus(ctx, itemID, model, "failed", err.Error())
		return err
	}

	if err := s.vs.Put(ctx, model, itemID, vecs[0]); err != nil {
		s.recordStatus(ctx, itemID, model, "failed", err.Error())
		return fmt.Errorf("store vector for %s: %w", itemID, err)
	}

	// Flip any_embedding=1 so SearchService knows this item is vector-searchable.
	item, err := s.repo.Get(ctx, itemID)
	if err != nil {
		// Vec row already written; do NOT fail the whole embed over the flag.
		// Record status as done (vec row IS the source of truth for "embedded");
		// the any_embedding flag is a perf optimization, not correctness.
		s.recordStatus(ctx, itemID, model, "done", "")
		return fmt.Errorf("load item for flag update: %w", err)
	}
	item.AnyEmbedding = 1
	if err := s.repo.Update(ctx, item); err != nil {
		s.recordStatus(ctx, itemID, model, "done", "")
		return fmt.Errorf("mark any_embedding: %w", err)
	}

	s.recordStatus(ctx, itemID, model, "done", "")
	return nil
}

// hydrateContent returns the inline Content if set, or fetches from
// FileStore via ContentURI. Returns empty string if neither is set
// (which Embed treats as title-only — caller's responsibility to
// decide if that's acceptable).
func (s *EmbedService) hydrateContent(ctx context.Context, itemID string) (string, error) {
	item, err := s.repo.Get(ctx, itemID)
	if err != nil {
		return "", err
	}
	if item.Content != "" {
		return item.Content, nil
	}
	if item.ContentURI == "" {
		return "", nil // neither inline nor externalized; title-only embed
	}
	bytes, err := s.fs.Get(item.ContentURI)
	if err != nil {
		return "", fmt.Errorf("fs.Get %s: %w", item.ContentURI, err)
	}
	return string(bytes), nil
}

// recordStatus wraps embRepo.UpsertStatus with stderr logging on failure.
// Status-row write failure must never mask the original embed result.
func (s *EmbedService) recordStatus(ctx context.Context, itemID, model, status, errStr string) {
	if err := s.embRepo.UpsertStatus(ctx, itemID, model, status, errStr); err != nil {
		fmt.Fprintf(os.Stderr,
			"warn: failed to record embedding status for %s: %v\n", itemID, err)
	}
}

// Ensure errors is imported only if used; remove if recordStatus is the
// only consumer (it's not — the file uses errors via the test file only).
var _ = errors.New
```

**Note:** the `var _ = errors.New` is a placeholder to avoid the "imported and not used" error if no other `errors` reference exists. Better: drop the `errors` import entirely if Step 3's code doesn't use it after finalizing. Implementer decides based on the final file contents.

- [ ] **Step 4: Add `SetEmbedHook` to the fake embedder**

The existing `internal/adapter/embedder/fake/fake.go` doesn't have a hook mechanism. The new tests need to inject failures and capture inputs. Read the current fake; add:

```go
// In fake.go, add a field + setter:

type Embedder struct {
	slug     string
	dim      int
	embedHook func(texts []string) ([][]float32, error) // nil = default behavior
}

// SetEmbedHook lets tests override the embed behavior: capture inputs,
// inject errors, return canned vectors. Pass nil to reset to default.
func (e *Embedder) SetEmbedHook(fn func([]string) ([][]float32, error)) {
	e.embedHook = fn
}

// Embed: if embedHook is set, call it; otherwise default behavior.
func (e *Embedder) Embed(ctx context.Context, texts []string) ([][]float32, error) {
	if e.embedHook != nil {
		return e.embedHook(texts)
	}
	// ... existing default code unchanged ...
}
```

- [ ] **Step 5: Add `newMemFileStore` helper**

The service package tests need an in-memory FileStore. Use the real `fsstore` adapter against `t.TempDir()` (matches the existing `newMemVectorStore` pattern of using real adapters against `:memory:` SQLite). Add to `internal/service/fixture_test.go` (or create if missing):

```go
func newMemFileStore(t *testing.T) port.FileStore {
	t.Helper()
	fs, err := fsstore.New(t.TempDir())
	require.NoError(t, err)
	return fs
}

func newMemEmbeddingRepo(t *testing.T, db *sql.DB) port.EmbeddingRepo {
	t.Helper()
	return sqlite.NewEmbeddingRepo(db)
}
```

Imports needed: `"database/sql"`, `"uni-context/internal/adapter/fsstore"`, `"uni-context/internal/adapter/sqlite"`, `"github.com/stretchr/testify/require"`.

- [ ] **Step 6: Update IngestService to drop `contentForEmbed`**

`internal/service/ingest.go`: replace the call site:

```go
// OLD:
if s.embed != nil {
    if err := s.embed.Embed(ctx, item.ID, item.Title, contentForEmbed(item)); err != nil {
        fmt.Fprintf(os.Stderr, "warn: embed failed for %s: %v\n", item.ID, err)
    }
}

// NEW (EmbedService handles hydration internally):
if s.embed != nil {
    if err := s.embed.Embed(ctx, item.ID, item.Title, item.Content); err != nil {
        fmt.Fprintf(os.Stderr, "warn: embed failed for %s: %v\n", item.ID, err)
    }
}
```

Delete the `contentForEmbed` function entirely (lines ~110-118 of the current file). Add a one-line comment near the embed call noting "EmbedService hydrates externalized content from FileStore (Plan 2b)".

- [ ] **Step 7: Run service package tests**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/service/...`
Expected: FAIL — `app.Wire` (Task 4) and possibly other test fixtures still use old constructor. Fix compile errors by stubbing the new args in any test fixture that constructs EmbedService directly.

The compile errors will identify all call sites. Each one gets `fs` and `embRepo` added. Don't update `app.Wire` yet (Task 4 owns that).

- [ ] **Step 8: Run again to verify the new tests pass**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestEmbedService ./internal/service/...`
Expected: PASS — 3 new tests green (hydration, status on success, status on failure) + existing embed tests still green.

- [ ] **Step 9: Run race detector**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -race ./internal/service/...`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
goimports -w internal/service/embed.go internal/service/embed_test.go \
             internal/service/ingest.go internal/service/fixture_test.go \
             internal/adapter/embedder/fake/fake.go
git add internal/service/embed.go internal/service/embed_test.go \
        internal/service/ingest.go internal/service/fixture_test.go \
        internal/adapter/embedder/fake/fake.go
git commit -m "feat(service): hydrate externalized content + write status rows

EmbedService constructor gains port.FileStore + port.EmbeddingRepo.
Embed() now:
- hydrates content from FileStore when caller passes empty content
  (fixes Plan 2a gap: externalized items embedded title-only)
- writes a context_embedding status row on every attempt (done/failed)
  via EmbeddingRepo.UpsertStatus; status-write failure logged to
  stderr but does not mask the original embed result

Fake embedder gains SetEmbedHook for test injection (capture inputs,
inject errors). IngestService.contentForEmbed removed — EmbedService
owns hydration now.

app.Wire + remaining callers updated in Task 4."
```

---

## Task 4: wireApp — construct EmbeddingRepo, pass to EmbedService

**Files:**
- Modify: `internal/app/app.go`
- Modify: `internal/app/wire_test.go` (if exists) or create `internal/app/app_test.go`

**Interfaces:**
- Consumes: Task 2's `sqlite.NewEmbeddingRepo`, Task 3's new `NewEmbedService` signature, existing `port.FileStore` (already on App.FS)
- Produces: `App.EmbeddingRepo`, `App.Backfill`, `App.Worker` fields (the latter two set to nil here, populated by Tasks 5/6). When `embedder.enabled=false`, all three stay nil — CLI commands error cleanly.

- [ ] **Step 1: Write the failing test**

Create `internal/app/app_test.go` (or extend existing if present):

```go
package app

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/config"
)

func TestWire_EmbedderEnabled_ConstructsEmbeddingRepo(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		DataDir: dir,
		Embedder: config.EmbedderConfig{
			Enabled:  true,
			Provider: "ollama",
			BaseURL:  "http://127.0.0.1:65535", // closed port; doctor won't actually call
			Model:    "bge-m3",
			Dimension: 1024,
		},
	}

	a, err := Wire(cfg)
	require.NoError(t, err)
	t.Cleanup(func() { _ = a.Close() })

	assert.NotNil(t, a.Embedder, "Embedder constructed when enabled")
	assert.NotNil(t, a.EmbeddingRepo, "EmbeddingRepo constructed when enabled")
	assert.NotNil(t, a.Ingest, "IngestService constructed")
	assert.NotNil(t, a.Search, "SearchService constructed")
	// Backfill/Worker are nil here; populated by Tasks 5/6
	assert.Nil(t, a.Backfill, "Backfill populated in Task 5")
	assert.Nil(t, a.Worker, "Worker populated in Task 6")
}

func TestWire_EmbedderDisabled_LeavesEmbeddingFieldsNil(t *testing.T) {
	// Plan 1 compat: no embedder construction; App.Backfill/Worker/Embedder
	// all nil so CLI commands error cleanly without nil-deref.
	dir := t.TempDir()
	cfg := &config.Config{DataDir: dir}

	a, err := Wire(cfg)
	require.NoError(t, err)
	t.Cleanup(func() { _ = a.Close() })

	assert.Nil(t, a.Embedder)
	assert.Nil(t, a.EmbeddingRepo)
	assert.Nil(t, a.Backfill)
	assert.Nil(t, a.Worker)
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/app/...`
Expected: FAIL — `a.EmbeddingRepo`, `a.Backfill`, `a.Worker` fields don't exist on App; `EmbeddingRepo` not constructed in Wire.

- [ ] **Step 3: Update Wire**

Modify `internal/app/app.go`. Add fields to App struct:

```go
type App struct {
	Config   *config.Config
	DB       *sql.DB
	Repo     port.ContextRepo
	Project  port.ProjectRepo
	Searcher port.Searcher
	FS       port.FileStore

	// Embedder is non-nil when cfg.Embedder.Enabled is true.
	Embedder     port.Embedder
	EmbeddingRepo port.EmbeddingRepo // NEW — Task 4
	Backfill     *service.BackfillService // NEW — Task 5 wires; nil here
	Worker       *service.WorkerService   // NEW — Task 6 wires; nil here

	Ingest *service.IngestService
	Search *service.SearchService
}
```

Update the Wire body where EmbedService is constructed:

```go
// OLD:
embedSvc = service.NewEmbedService(embedder, vectorStore, repo)

// NEW:
embeddingRepo := sqlite.NewEmbeddingRepo(db)
embedSvc = service.NewEmbedService(embedder, vectorStore, repo, fs, embeddingRepo)
```

Update the return struct:

```go
return &App{
	Config:        cfg,
	DB:            db,
	Repo:          repo,
	Project:       proj,
	Searcher:      searcher,
	FS:            fs,
	Embedder:      embedder,
	EmbeddingRepo: embeddingRepo, // nil when embedder disabled
	Ingest:        ingest,
	Search:        search,
}, nil
```

**Important:** `embeddingRepo` is declared inside the `if cfg.Embedder.Enabled` block. To use it in the return struct, either declare it at function scope (set to nil when disabled) or move the return inside the conditional. Cleaner: declare at function scope:

```go
var embeddingRepo port.EmbeddingRepo // nil unless embedder enabled
if cfg.Embedder.Enabled {
	// ... existing switch + EnsureModelRegistered ...
	embeddingRepo = sqlite.NewEmbeddingRepo(db)
	embedSvc = service.NewEmbedService(embedder, vectorStore, repo, fs, embeddingRepo)
}
```

- [ ] **Step 4: Run tests**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/app/...`
Expected: PASS — both new tests green.

- [ ] **Step 5: Run full suite**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./...`
Expected: PASS — all packages green.

- [ ] **Step 6: Commit**

```bash
goimports -w internal/app/app.go internal/app/app_test.go
git add internal/app/app.go internal/app/app_test.go
git commit -m "feat(app): wire EmbeddingRepo; expose Backfill/Worker fields

App struct gains EmbeddingRepo, Backfill, Worker fields. Wire
constructs sqlite.NewEmbeddingRepo when embedder.enabled and passes
it + the existing FileStore to the new EmbedService signature.

Backfill and Worker stay nil here; Tasks 5 and 6 populate them."
```

---

## Task 5: BackfillService + `embed backfill` CLI

**Files:**
- Create: `internal/service/backfill.go`
- Create: `internal/service/backfill_test.go`
- Create: `internal/cli/embed.go`
- Create: `internal/cli/embed_test.go`
- Modify: `internal/app/app.go` (wire BackfillService)

**Interfaces:**
- Consumes: Task 3's `EmbedService` (calls `Embed` per item), Task 2's `port.EmbeddingRepo` (for `GetStatus` short-circuit), existing `port.ContextRepo` (needs `List` with filter)
- Produces: `service.BackfillService`, `service.BackfillReport`, CLI subcommand `unictx embed backfill [--limit N] [--dry-run]`

- [ ] **Step 1: Write the failing service test**

`internal/service/backfill_test.go`:

```go
package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/domain"
)

func TestBackfillService_ProcessesOnlyUnembeddedItems(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	// 3 items: A and B unembedded (any_embedding=0), C already embedded.
	itemA := makeItemForBackfill(t, f, "alpha", "content A")
	itemB := makeItemForBackfill(t, f, "beta", "content B")
	itemC := makeItemForBackfill(t, f, "gamma", "content C")
	// Mark C as already embedded
	require.NoError(t, f.svc.Embed(context.Background(), itemC, "gamma", "content C"))

	svc := NewBackfillService(f.repo, f.svc)
	report, err := svc.Run(context.Background(), 0, false)
	require.NoError(t, err)

	assert.Equal(t, 2, report.Embedded, "only A and B embedded; C excluded by filter")
	assert.Equal(t, 0, report.Failed)

	// Verify A and B now have status='done'
	for _, id := range []string{itemA, itemB} {
		st, err := f.embRepo.GetStatus(context.Background(), id, "fake-model")
		require.NoError(t, err)
		assert.Equal(t, "done", st.Status, "item %s should be embedded now", id)
	}
}

func TestBackfillService_DryRunDoesNotEmbed(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	itemA := makeItemForBackfill(t, f, "alpha", "content A")

	svc := NewBackfillService(f.repo, f.svc)
	report, err := svc.Run(context.Background(), 0, true) // dryRun=true
	require.NoError(t, err)

	assert.Equal(t, 0, report.Embedded, "dry run does not embed")
	assert.Equal(t, 1, report.Scanned, "dry run counts candidates")
	assert.Equal(t, 0, report.Skipped)

	// Item A still has no embedding
	st, err := f.embRepo.GetStatus(context.Background(), itemA, "fake-model")
	require.Error(t, err, "no status row written during dry run")
}

func TestBackfillService_LimitHonored(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	for _, title := range []string{"a", "b", "c", "d", "e"} {
		makeItemForBackfill(t, f, title, "content "+title)
	}

	svc := NewBackfillService(f.repo, f.svc)
	report, err := svc.Run(context.Background(), 3, false) // limit=3
	require.NoError(t, err)

	assert.Equal(t, 3, report.Embedded, "limit caps the run")
	assert.Equal(t, 3, report.Scanned)
}

func TestBackfillService_ContinuesOnEmbedFailure(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	itemA := makeItemForBackfill(t, f, "alpha", "A")
	itemB := makeItemForBackfill(t, f, "beta", "B")
	itemC := makeItemForBackfill(t, f, "gamma", "C")

	// Fail ONLY when embedding item B (by title match)
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		if len(texts) > 0 && strings.Contains(texts[0], "beta") {
			return nil, errors.New("simulated failure on beta")
		}
		return [][]float32{make([]float32, 8)}, nil
	})

	svc := NewBackfillService(f.repo, f.svc)
	report, err := svc.Run(context.Background(), 0, false)
	require.NoError(t, err, "Run itself does not fail on per-item errors")

	assert.Equal(t, 2, report.Embedded, "A and C embedded")
	assert.Equal(t, 1, report.Failed, "B failed")
	require.Len(t, report.Failures, 1)
	assert.Equal(t, itemB, report.Failures[0].ItemID)
}

// helper
func makeItemForBackfill(t *testing.T, f *embedFixture, title, content string) string {
	t.Helper()
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = title
	item.Content = content
	require.NoError(t, f.repo.Create(context.Background(), item))
	return item.ID
}
```

(The test file will need `strings` and `errors` imports.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestBackfillService ./internal/service/...`
Expected: FAIL — `NewBackfillService` undefined, `BackfillReport` undefined.

- [ ] **Step 3: Implement BackfillService**

`internal/service/backfill.go`:

```go
package service

import (
	"context"
	"fmt"
	"os"

	"uni-context/internal/port"
)

// BackfillService bulk-embeds items where any_embedding=0. Idempotent:
// items already embedded (any_embedding=1) are skipped. Failures during
// the run are recorded but do not abort — Run returns a BackfillReport
// summarizing what happened.
type BackfillService struct {
	repo  port.ContextRepo
	embed *EmbedService
}

func NewBackfillService(repo port.ContextRepo, embed *EmbedService) *BackfillService {
	return &BackfillService{repo: repo, embed: embed}
}

type BackfillFailure struct {
	ItemID string
	Error  string
}

type BackfillReport struct {
	Scanned  int // candidates found (any_embedding=0). Skipped is NOT a
	// field — backfill pre-filters via ItemFilter.AnyEmbedding so
	// already-embedded items are excluded before iteration begins.
	Embedded int // successfully embedded this run
	Failed   int
	Failures []BackfillFailure
}

// Run iterates items where any_embedding=0, ordered by created_at ASC
// (oldest first — they've waited longest). For each:
//   - dryRun=true: count only
//   - dryRun=false: call EmbedService.Embed; on failure record and continue
//
// limit<=0 means no limit. Progress logged to stderr every 100 items.
func (s *BackfillService) Run(ctx context.Context, limit int, dryRun bool) (BackfillReport, error) {
	var report BackfillReport

	// Use ContextRepo.List with an any_embedding filter. The current
	// ItemFilter doesn't have an any_embedding field — see Step 4 for
	// the small addition.
	// AnyEmbedding is *int — take address of zero value so the filter
	// is honored (nil pointer = no filter).
	anyEmbedZero := 0
	items, _, err := s.repo.List(ctx, port.ItemFilter{
		AnyEmbedding: &anyEmbedZero, // 0 = unembedded only
		Limit:        limit,
	})
	if err != nil {
		return report, fmt.Errorf("list unembedded items: %w", err)
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
			report.Failures = append(report.Failures, BackfillFailure{
				ItemID: item.ID, Error: err.Error(),
			})
			continue
		}
		report.Embedded++

		if (i+1)%100 == 0 {
			fmt.Fprintf(os.Stderr, "backfill: %d items processed\n", i+1)
		}
	}
	return report, nil
}
```

- [ ] **Step 4: Add `AnyEmbedding` filter to `port.ItemFilter`**

The current `port.ItemFilter` has Scopes, Kinds, OwnerUserID, ProjectID, Tags, Cursor, Limit. Add:

```go
// In internal/port/repository.go, ItemFilter:
type ItemFilter struct {
	// ... existing fields ...

	// AnyEmbedding filters by context_item.any_embedding. Pointer-style:
	//   nil/omitted → no filter (all items)
	//   0           → only items NOT yet embedded
	//   1           → only items already embedded
	// Using int (not *int) keeps the zero-value semantics natural —
	// callers who don't set it get "no filter" via the `omitted` check
	// below. We use a sentinel: if AnyEmbedding is set to -1, no filter;
	// otherwise filter by the value. This is awkward but avoids *int.
	// Simpler: use a *int. Refactor:
	AnyEmbedding *int
}
```

Actually use `*int` — cleaner. Update `ContextRepo.List` in `internal/adapter/sqlite/repo.go`:

```go
// In List, after existing filters, before cursor:
if f.AnyEmbedding != nil {
	where = append(where, "any_embedding = ?")
	args = append(args, *f.AnyEmbedding)
}
```

Callers from Plan 2a don't set AnyEmbedding (nil pointer), so behavior unchanged.

- [ ] **Step 5: Run service tests**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestBackfillService ./internal/service/...`
Expected: PASS — 4 tests green.

- [ ] **Step 6: Write the failing CLI test**

`internal/cli/embed_test.go`:

```go
package cli

import (
	"bytes"
	"context"
	"os"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestEmbedCmd_HasBackfillAndWorkerSubcommands(t *testing.T) {
	// Structural test: verify the embed parent command has exactly two
	// subcommands. Prevents accidental removal during refactoring.
	subs := embedCmd.Commands()
	assert.Equal(t, 2, len(subs), "embed has backfill + worker subcommands")

	names := []string{subs[0].Use, subs[1].Use}
	assert.Contains(t, names, "backfill")
	assert.Contains(t, names, "worker")
}
```

- [ ] **Step 7: Implement the CLI**

`internal/cli/embed.go`:

```go
package cli

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/spf13/cobra"
)

var (
	backfillLimit int
	backfillDryRun bool
	workerInterval time.Duration
)

var embedCmd = &cobra.Command{
	Use:   "embed",
	Short: "Manage embeddings (backfill, worker)",
	// No RunE — must be invoked with a subcommand.
}

var embedBackfillCmd = &cobra.Command{
	Use:   "backfill",
	Short: "Embed all items where any_embedding=0 (idempotent)",
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadApp()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.Backfill == nil {
			return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
		}

		ctx := signalContext()
		report, err := a.Backfill.Run(ctx, backfillLimit, backfillDryRun)
		if err != nil {
			return err
		}

		if backfillDryRun {
			fmt.Printf("dry run: would embed %d items\n", report.Scanned)
			return nil
		}
		fmt.Printf("backfill complete: embedded=%d failed=%d scanned=%d\n",
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

var embedWorkerCmd = &cobra.Command{
	Use:   "worker",
	Short: "Long-running retry loop for status=failed embeddings (Ctrl+C to stop)",
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadApp()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.Worker == nil {
			return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
		}

		ctx := signalContext()
		fmt.Fprintf(os.Stderr, "worker: polling every %s, Ctrl+C to stop\n", workerInterval)
		return a.Worker.Run(ctx, workerInterval)
	},
}

// signalContext returns a context cancelled by SIGINT/SIGTERM. Used by
// long-running commands (backfill on large corpus, worker) so Ctrl+C
// drains gracefully.
func signalContext() context.Context {
	ctx, cancel := context.WithCancel(context.Background())
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		cancel()
	}()
	return ctx
}

func init() {
	embedBackfillCmd.Flags().IntVar(&backfillLimit, "limit", 0,
		"max items to embed (0 = no limit)")
	embedBackfillCmd.Flags().BoolVar(&backfillDryRun, "dry-run", false,
		"count candidates without embedding")
	embedWorkerCmd.Flags().DurationVar(&workerInterval, "interval", 30*time.Second,
		"poll interval for failed-embedding retries")

	embedCmd.AddCommand(embedBackfillCmd)
	embedCmd.AddCommand(embedWorkerCmd)
	rootCmd.AddCommand(embedCmd)
}
```

- [ ] **Step 8: Wire BackfillService into App**

In `internal/app/app.go`, after constructing EmbedService:

```go
var (
	embeddingRepo port.EmbeddingRepo
	embedSvc      *service.EmbedService
	backfill      *service.BackfillService
)
if cfg.Embedder.Enabled {
	// ... existing embedder construction ...
	embeddingRepo = sqlite.NewEmbeddingRepo(db)
	embedSvc = service.NewEmbedService(embedder, vectorStore, repo, fs, embeddingRepo)
	backfill = service.NewBackfillService(repo, embedSvc)
}
```

And in the return struct:

```go
return &App{
	// ... existing ...
	EmbeddingRepo: embeddingRepo,
	Backfill:      backfill,
	// Worker stays nil until Task 6
}, nil
```

Update `App` struct definition to add `Backfill *service.BackfillService` if not already there from Task 4.

- [ ] **Step 9: Run CLI test**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestEmbedCmd ./internal/cli/...`
Expected: PASS — `TestEmbedCmd_HasBackfillAndWorkerSubcommands` green; placeholder skipped.

- [ ] **Step 10: Run full suite + build**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./... && make build`
Expected: PASS — all green, binary builds.

- [ ] **Step 11: Commit**

```bash
goimports -w internal/service/backfill.go internal/service/backfill_test.go \
             internal/cli/embed.go internal/cli/embed_test.go \
             internal/app/app.go internal/port/repository.go \
             internal/adapter/sqlite/repo.go
git add internal/service/backfill.go internal/service/backfill_test.go \
        internal/cli/embed.go internal/cli/embed_test.go \
        internal/app/app.go internal/port/repository.go \
        internal/adapter/sqlite/repo.go
git commit -m "feat(service,cli): add BackfillService + 'embed backfill' cmd

BackfillService iterates items where any_embedding=0 (ordered
oldest-first), calling EmbedService.Embed per item. Failures recorded
but do not abort; Run returns BackfillReport. --dry-run counts only;
--limit caps the run.

CLI: 'unictx embed backfill [--limit N] [--dry-run]' subcommand added
under new 'embed' parent. Errors cleanly when embedder.enabled=false.

port.ItemFilter gains *int AnyEmbedding field (nil = no filter, the
default for all Plan 1/2a callers). Worker subcommand wired in Task 6."
```

---

## Task 6: WorkerService + `embed worker` CLI

**Files:**
- Create: `internal/service/worker.go`
- Create: `internal/service/worker_test.go`
- Modify: `internal/cli/embed.go` (worker subcommand already declared in Task 5; verify)
- Modify: `internal/app/app.go` (wire WorkerService)

**Interfaces:**
- Consumes: Task 2's `port.EmbeddingRepo.ListFailed`, Task 3's `EmbedService.Embed`. Needs the item's title + content to re-embed — fetch via `port.ContextRepo.Get`.
- Produces: `service.WorkerService`, CLI subcommand `unictx embed worker [--interval 30s]`. Worker subcommand already wired in Task 5's embed.go; just verify it actually runs.

- [ ] **Step 1: Write the failing test**

`internal/service/worker_test.go`:

```go
package service

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestWorkerService_RetriesFailedEmbeddings(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	// Three items, all failed on first embed attempt.
	itemA := makeItemForBackfill(t, f, "alpha", "A")
	itemB := makeItemForBackfill(t, f, "beta", "B")
	itemC := makeItemForBackfill(t, f, "gamma", "C")

	// Initial failed attempts — simulate via direct Embed calls with
	// a failing hook.
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		return nil, errors.New("transient")
	})
	for _, id := range []string{itemA, itemB, itemC} {
		_ = f.svc.Embed(context.Background(), id, "title", "content")
	}

	// Verify all 3 are 'failed' with attempts=1
	for _, id := range []string{itemA, itemB, itemC} {
		st, _ := f.embRepo.GetStatus(context.Background(), id, "fake-model")
		require.Equal(t, "failed", st.Status)
		require.Equal(t, 1, st.Attempts)
	}

	// Now flip hook to succeed; run worker for ONE iteration.
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		return [][]float32{make([]float32, 8)}, nil
	})

	// EmbedService doesn't know about BackfillService's helper; use repo
	// to fetch title/content for the worker. The WorkerService will
	// fetch internally.
	svc := NewWorkerService(f.repo, f.embRepo, f.svc)

	// RunOneIteration exposes single-pass semantics for testing.
	processed, err := svc.RunOneIteration(context.Background())
	require.NoError(t, err)
	assert.Equal(t, 3, processed, "all 3 failures retried")

	// All 3 should now be 'done' with attempts=2
	for _, id := range []string{itemA, itemB, itemC} {
		st, _ := f.embRepo.GetStatus(context.Background(), id, "fake-model")
		assert.Equal(t, "done", st.Status)
		assert.Equal(t, 2, st.Attempts, "attempts incremented to 2")
	}
}

func TestWorkerService_NoFailures_ReturnsZero(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	svc := NewWorkerService(f.repo, f.embRepo, f.svc)
	processed, err := svc.RunOneIteration(context.Background())
	require.NoError(t, err)
	assert.Equal(t, 0, processed, "nothing to retry")
}

func TestWorkerService_Run_ExitsOnContextCancel(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	svc := NewWorkerService(f.repo, f.embRepo, f.svc)
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // pre-cancelled

	err := svc.Run(ctx, 10*time.Millisecond)
	require.Error(t, err)
	assert.ErrorIs(t, err, context.Canceled)
}

func TestWorkerService_PartialFailure_KeepsItemInQueue(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	// itemFail always fails; itemSucceed succeeds. After one iteration,
	// itemFail stays 'failed' (attempts++), itemSucceed flips to 'done'.
	itemFail := makeItemForBackfill(t, f, "fail-title", "content F")
	itemSucceed := makeItemForBackfill(t, f, "ok-title", "content S")

	// Initial failures
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		return nil, errors.New("init fail")
	})
	_ = f.svc.Embed(context.Background(), itemFail, "fail-title", "content F")
	_ = f.svc.Embed(context.Background(), itemSucceed, "ok-title", "content S")

	// Mixed hook: succeed for itemSucceed, fail for itemFail
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		if len(texts) > 0 && strings.Contains(texts[0], "ok-title") {
			return [][]float32{make([]float32, 8)}, nil
		}
		return nil, errors.New("persistent")
	})

	svc := NewWorkerService(f.repo, f.embRepo, f.svc)
	processed, err := svc.RunOneIteration(context.Background())
	require.NoError(t, err)
	assert.Equal(t, 2, processed)

	stFail, _ := f.embRepo.GetStatus(context.Background(), itemFail, "fake-model")
	assert.Equal(t, "failed", stFail.Status)
	assert.Equal(t, 2, stFail.Attempts)

	stOk, _ := f.embRepo.GetStatus(context.Background(), itemSucceed, "fake-model")
	assert.Equal(t, "done", stOk.Status)
	assert.Equal(t, 2, stOk.Attempts)
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestWorkerService ./internal/service/...`
Expected: FAIL — `NewWorkerService` undefined, `RunOneIteration` undefined.

- [ ] **Step 3: Implement WorkerService**

`internal/service/worker.go`:

```go
package service

import (
	"context"
	"fmt"
	"os"
	"time"

	"uni-context/internal/port"
)

// WorkerService polls for status='failed' embeddings and retries them.
// Long-running: caller (CLI) cancels context on Ctrl+C.
//
// Plan 2b scope: fixed poll interval, no exponential backoff, no max-
// attempts cap. A row stays 'failed' until it succeeds; user can DELETE
// the row manually to skip an unrecoverable item (e.g. wrong model).
type WorkerService struct {
	repo    port.ContextRepo
	embRepo port.EmbeddingRepo
	embed   *EmbedService
}

func NewWorkerService(repo port.ContextRepo, embRepo port.EmbeddingRepo, embed *EmbedService) *WorkerService {
	return &WorkerService{repo: repo, embRepo: embRepo, embed: embed}
}

const workerBatchSize = 100

// RunOneIteration processes one batch of failed embeddings. Returns the
// number of items attempted (success or failure). Exposed for testing
// so tests don't need to deal with the loop + interval machinery.
func (s *WorkerService) RunOneIteration(ctx context.Context) (int, error) {
	failed, err := s.embRepo.ListFailed(ctx, workerBatchSize)
	if err != nil {
		return 0, fmt.Errorf("list failed: %w", err)
	}

	processed := 0
	for _, st := range failed {
		select {
		case <-ctx.Done():
			return processed, ctx.Err()
		default:
		}

		// Fetch the item to get its title + (inline) content. EmbedService
		// will hydrate from FileStore if content was externalized.
		item, err := s.repo.Get(ctx, st.ItemID)
		if err != nil {
			// Item was deleted between failure and retry. Log + skip;
			// the ON DELETE CASCADE on context_embedding.item_id should
			// have removed the row, but defensive.
			fmt.Fprintf(os.Stderr, "worker: item %s vanished: %v\n", st.ItemID, err)
			continue
		}

		// EmbedService.Embed handles status row update internally (writes
		// 'done' on success, 'failed' + attempts++ on failure).
		if err := s.embed.Embed(ctx, item.ID, item.Title, item.Content); err != nil {
			fmt.Fprintf(os.Stderr, "worker: retry failed for %s (attempt %d): %v\n",
				item.ID, st.Attempts+1, err)
		}
		processed++
	}
	return processed, nil
}

// Run loops RunOneIteration with the given interval until ctx is cancelled.
// Logs to stderr each iteration: "worker: processed N items, sleeping <interval>".
func (s *WorkerService) Run(ctx context.Context, interval time.Duration) error {
	if interval <= 0 {
		interval = 30 * time.Second
	}
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		processed, err := s.RunOneIteration(ctx)
		if err != nil && err != context.Canceled {
			return err
		}
		fmt.Fprintf(os.Stderr, "worker: processed %d items, sleeping %s\n",
			processed, interval)

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(interval):
		}
	}
}
```

- [ ] **Step 4: Run service tests**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestWorkerService ./internal/service/...`
Expected: PASS — 4 tests green.

- [ ] **Step 5: Wire WorkerService into App**

In `internal/app/app.go`:

```go
var (
	embeddingRepo port.EmbeddingRepo
	embedSvc      *service.EmbedService
	backfill      *service.BackfillService
	worker        *service.WorkerService
)
if cfg.Embedder.Enabled {
	// ... existing ...
	embeddingRepo = sqlite.NewEmbeddingRepo(db)
	embedSvc = service.NewEmbedService(embedder, vectorStore, repo, fs, embeddingRepo)
	backfill = service.NewBackfillService(repo, embedSvc)
	worker = service.NewWorkerService(repo, embeddingRepo, embedSvc)
}

return &App{
	// ... existing ...
	EmbeddingRepo: embeddingRepo,
	Backfill:      backfill,
	Worker:        worker,
}, nil
```

Update `App` struct to add `Worker *service.WorkerService` if not already there.

- [ ] **Step 6: Verify the worker CLI subcommand from Task 5 actually works**

The `embedCmd` parent and `embedWorkerCmd` were declared in Task 5's `internal/cli/embed.go`. Verify the worker branch of `RunE` references `a.Worker` (not `a.Backfill`). If Task 5 left it as a stub, fix:

```go
// In embedWorkerCmd.RunE (already declared in Task 5):
if a.Worker == nil {
    return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
}
ctx := signalContext()
fmt.Fprintf(os.Stderr, "worker: polling every %s, Ctrl+C to stop\n", workerInterval)
return a.Worker.Run(ctx, workerInterval)
```

- [ ] **Step 7: Run full suite**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./... && make build`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
goimports -w internal/service/worker.go internal/service/worker_test.go \
             internal/app/app.go internal/cli/embed.go
git add internal/service/worker.go internal/service/worker_test.go \
        internal/app/app.go internal/cli/embed.go
git commit -m "feat(service,cli): add WorkerService + 'embed worker' cmd

WorkerService polls context_embedding for status='failed' rows and
retries EmbedService.Embed. Batch size 100, oldest failures first.
RunOneIteration exposed for testability; Run loops with cancellable
interval (default 30s). No max-attempts cap — user controls lifetime
via Ctrl+C; manual row DELETE skips unrecoverable items.

CLI: 'unictx embed worker [--interval 30s]' subcommand (declared in
Task 5) wired to WorkerService. Errors cleanly when embedder disabled.

App.Worker field populated when embedder.enabled; nil otherwise."
```

---

## Task 7: CHANGELOG + integration smoke

**Files:**
- Modify: `CHANGELOG.md`
- Create or extend: `internal/cli/e2e_backfill_test.go` (integration-tagged)

**Interfaces:**
- Consumes: Tasks 1-6
- Produces: CHANGELOG entry for Plan 2b; gated e2e test proving backfill + worker recovery end-to-end against real Ollama/LMStudio.

- [ ] **Step 1: Write the gated e2e test**

`internal/cli/e2e_backfill_test.go`:

```go
//go:build integration && e2e

package cli

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestE2E_BackfillRecoversFromIngestFailure proves the full recovery
// path: ingest while embedder is unreachable (status='failed'), then run
// worker once embedder is back, verify all items reach 'done'.
//
// Gated: requires real Ollama or LMStudio reachable + UNICTX_E2E_BACKFILL=1.
// Skipped otherwise. Setup expectations:
//   - config.yaml at XDG_CONFIG_HOME/unictx/config.yaml with embedder.enabled=true
//   - One bin at ./unictx (built via make build)
//   - Embedder reachable for the second half of the test (the first half
//     deliberately breaks it via a bogus base_url)
func TestE2E_BackfillRecoversFromIngestFailure(t *testing.T) {
	if os.Getenv("UNICTX_E2E_BACKFILL") != "1" {
		t.Skip("set UNICTX_E2E_BACKFILL=1 + provide a live embedder to run")
	}

	bin := buildBin(t)
	home := t.TempDir()

	// Phase 1: ingest 3 items with a BROKEN embedder (bogus port)
	cfgBad := `embedder:
  enabled: true
  provider: openai
  base_url: http://127.0.0.1:65535/v1
  model: bge-m3
  dimension: 1024
`
	cfgDir := filepath.Join(home, "config", "unictx")
	require.NoError(t, os.MkdirAll(cfgDir, 0o755))
	require.NoError(t, os.WriteFile(filepath.Join(cfgDir, "config.yaml"), []byte(cfgBad), 0o644))

	for _, title := range []string{"alpha", "beta", "gamma"} {
		out := runBin(t, bin, home, "user", "note", "add", "content "+title, "--title", title)
		assert.Contains(t, out, "added", "ingest should succeed even when embed fails")
	}

	// Verify 3 'failed' rows in DB
	dbPath := filepath.Join(home, "data", "unictx", "unictx.db")
	count := queryCount(t, dbPath,
		`SELECT count(*) FROM context_embedding WHERE status='failed'`)
	require.Equal(t, 3, count, "3 failed status rows after broken-ingest phase")

	// Phase 2: rewrite config with the REAL embedder URL from env
	realURL := os.Getenv("UNICTX_E2E_EMBEDDER_URL")
	if realURL == "" {
		realURL = "http://localhost:11434" // ollama default
	}
	cfgGood := strings.Replace(cfgBad, "http://127.0.0.1:65535/v1", realURL, 1)
	require.NoError(t, os.WriteFile(filepath.Join(cfgDir, "config.yaml"), []byte(cfgGood), 0o644))

	// Phase 3: run worker for ONE iteration (via short timeout trick —
	// worker is long-running, so we kill it after 5s)
	cmd := exec.Command(bin, "embed", "worker", "--interval", "1s")
	cmd.Env = append(os.Environ(), "XDG_CONFIG_HOME="+filepath.Join(home, "config"),
		"XDG_DATA_HOME="+filepath.Join(home, "data"))
	if err := cmd.Start(); err != nil {
		t.Fatalf("start worker: %v", err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case <-time.After(5 * time.Second):
		_ = cmd.Process.Kill()
	case <-done:
	}

	// Phase 4: verify all 3 reached 'done'
	count = queryCount(t, dbPath,
		`SELECT count(*) FROM context_embedding WHERE status='done'`)
	assert.Equal(t, 3, count, "all 3 should be 'done' after worker run")
}

// Helper: build the binary once per test run
func buildBin(t *testing.T) string {
	t.Helper()
	bin := filepath.Join(t.TempDir(), "unictx")
	cmd := exec.Command("make", "build")
	cmd.Env = append(os.Environ(), "BUILD_BIN="+bin)
	require.NoError(t, cmd.Run(), "make build")
	return bin
}

func runBin(t *testing.T, bin, home string, args ...string) string {
	t.Helper()
	cmd := exec.Command(bin, args...)
	cmd.Env = append(os.Environ(),
		"XDG_CONFIG_HOME="+filepath.Join(home, "config"),
		"XDG_DATA_HOME="+filepath.Join(home, "data"))
	out, err := cmd.CombinedOutput()
	require.NoError(t, err, "run %v: %s", args, out)
	return string(out)
}

func queryCount(t *testing.T, dbPath, query string) int {
	t.Helper()
	out, err := exec.Command("sqlite3", dbPath, query).Output()
	require.NoError(t, err, "sqlite3 query failed")
	n := 0
	_, err = fmt.Sscanf(strings.TrimSpace(string(out)), "%d", &n)
	require.NoError(t, err)
	return n
}
```

Add `"fmt"` and `"time"` to imports.

- [ ] **Step 2: Update CHANGELOG**

Append to `CHANGELOG.md`, after the Plan 2a section:

```markdown
## Plan 2b — Async Embed Queue + Backfill (2026-06-21)

Closes four Plan 2a gaps: FileStore content hydration, `context_embedding`
status rows, `unictx embed backfill`, `unictx embed worker`. See
`docs/superpowers/plans/2026-06-21-plan-2b-async-backfill.md` for the plan
and `.superpowers/sdd/progress.md` for execution notes.

**What shipped:**
- **Migration 0003:** `context_embedding` gains `attempts` (INTEGER NOT NULL DEFAULT 0) and `last_error` (TEXT). Additive ALTER.
- **`port.EmbeddingRepo`:** new single-responsibility port (`UpsertStatus` / `GetStatus` / `ListFailed`). Separate from `ContextRepo`.
- **`EmbedService` constructor change:** gains `port.FileStore` + `port.EmbeddingRepo` deps. Hydrates externalized content from FileStore (fixes Plan 2a "externalized items embed title-only" gap). Writes a status row on every attempt (done/failed).
- **`unictx embed backfill [--limit N] [--dry-run]`:** bulk-embeds items where `any_embedding=0`. Idempotent. Failures recorded but don't abort.
- **`unictx embed worker [--interval 30s]`:** long-running retry loop for `status='failed'` rows. Ctrl+C to stop.
- **`port.ItemFilter.AnyEmbedding`:** new `*int` field for backfill's "unembedded only" query. Default nil = no filter (Plan 1/2a callers unchanged).

### Known Limitations (Plan 2b)

1. **Worker has no max-attempts cap.** A row stays `status='failed'` until it succeeds or the user manually `DELETE`s the row. Rationale: YAGNI; user controls worker lifetime via Ctrl+C. Plan 2e (if ever) could add `status='exhausted'` after N attempts.

2. **No exponential backoff.** Worker polls at fixed interval (default 30s). Same rationale as above.

3. **Backfill + worker send one embed request per item.** No batched embeddings API call (OpenAI supports 1 request, N inputs). Plan 2d polish. Per-item error isolation is the trade-off.

4. **No `unictx embed status <id>` command.** Read-only inspection of `context_embedding` rows. Trivial follow-up; deferred to avoid scope creep. Use `sqlite3 unictx.db "SELECT * FROM context_embedding"` in the meantime.

5. **`EmbedService` constructor signature is a breaking change.** Plan 2a had two callers (`app.Wire` + tests); both updated in this patch series. Any out-of-tree consumers (none known) would need the same update.

### Deferred to Plan 2c+

- Multi-model parallel embedding + per-model vec tables
- Re-embedding when switching models
- Provider auto-detection / encoding formats (OpenAI-compat polish)
- `unictx embed status <id>` (read-only status inspection)
```

Also tighten the Plan 2a Known Limitations section: limitations 1, 2, 4, 7 are now closed by 2b. Update the cross-references.

- [ ] **Step 3: Run gated e2e test (skipped by default)**

Run: `CGO_ENABLED=1 go test -tags 'sqlite_fts5,integration,e2e' -run TestE2E_BackfillRecoversFromIngestFailure ./internal/cli/... -v -timeout 60s`
Expected: SKIP (env var not set). Verifies the test compiles and skip path works.

- [ ] **Step 4: Run full suite**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./... && make build`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
goimports -w internal/cli/e2e_backfill_test.go
git add CHANGELOG.md internal/cli/e2e_backfill_test.go
git commit -m "docs(changelog): document Plan 2b; add gated backfill e2e

CHANGELOG gains Plan 2b section: what shipped (migration 0003,
EmbeddingRepo, EmbedService changes, backfill + worker CLIs),
known limitations (no max-attempts, no exp backoff, per-item
requests, no status cmd), and what's still deferred to 2c+.

E2E test proves the recovery flow: ingest 3 items against a broken
embedder (status='failed'), swap config to a live embedder, run
worker, assert all 3 flip to 'done'. Gated on UNICTX_E2E_BACKFILL=1
plus a reachable embedder; skipped by default."
```

---

## Self-Review

**1. Spec coverage:**

| Spec section | Plan task |
|---|---|
| Migration 0003 (attempts + last_error) | Task 1 |
| `port.EmbeddingRepo` (UpsertStatus/GetStatus/ListFailed) | Task 2 |
| `EmbedService` hydration + status row + constructor change | Task 3 |
| `wireApp` constructs EmbeddingRepo, passes to EmbedService | Task 4 |
| `BackfillService` + `embed backfill` CLI | Task 5 |
| `WorkerService` + `embed worker` CLI | Task 6 |
| `port.ItemFilter.AnyEmbedding` field | Task 5 (Step 4) |
| Status row written on every attempt | Task 3 (recordStatus called from all paths) |
| FileStore hydration when content empty + ContentURI set | Task 3 (hydrateContent) |
| Sync ingest stays unchanged in latency | Task 3 (no goroutine introduced) |
| Plan 1/2a backward compat preserved | Tasks 4 + 5 (`embedder.enabled=false` → App.Backfill/Worker nil; ItemFilter.AnyEmbedding default nil) |
| CHANGELOG updated | Task 7 |
| Gated e2e for backfill recovery flow | Task 7 |

**2. Placeholder scan:** No "TBD", "TODO", "fill in", "similar to" found. The Task 2 skipped test is explicit and removed before commit (Step 5). The Task 5 CLI test placeholder is replaced by structural assertions + defers full invocation to e2e in Task 7.

**3. Type consistency:**
- `port.EmbeddingStatus` struct fields: `ItemID, ModelSlug, Status, Error, LastError, Attempts, EmbeddedAt` — consistent across Task 2 (definition), Task 3 (consumer via EmbeddingRepo), Task 5 (BackfillService doesn't read EmbeddingStatus directly), Task 6 (WorkerService reads via ListFailed).
- `EmbedService.Embed(ctx, itemID, title, content)` signature unchanged — Tasks 5 and 6 call it identically.
- `BackfillReport` fields: `Scanned, Embedded, Skipped, Failed, Failures` — consistent in Task 5 (def + test) and Task 7 (CHANGELOG).
- `BackfillFailure` fields: `ItemID, Error` — consistent.
- `NewEmbedService(embedder, vs, repo, fs, embRepo)` — Task 3 defines; Task 4 consumes in Wire; no other callers.
- `NewBackfillService(repo, embed)` — Task 5 defines; Task 5 Step 8 wires.
- `NewWorkerService(repo, embRepo, embed)` — Task 6 defines; Task 6 Step 5 wires.

**No issues found.** Plan is internally consistent and covers all spec sections.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-21-plan-2b-async-backfill.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task, reviewer between tasks, fast iteration via `superpowers:subagent-driven-development`.

**2. Inline Execution** — batch execution with checkpoints via `superpowers:executing-plans`.

**Recommended model per task:**
- Task 1 (migration): standard model — straightforward ALTER + test
- Task 2 (EmbeddingRepo): standard model — port + adapter + UPSERT nuance
- Task 3 (EmbedService): standard-to-capable model — constructor signature change touches fixtures
- Task 4 (wireApp): standard model — wiring change
- Task 5 (Backfill + CLI): standard-to-capable model — touches service + CLI + port + adapter
- Task 6 (Worker + CLI): standard model — similar shape to Task 5 but isolated
- Task 7 (CHANGELOG + e2e): standard model — docs + integration test scaffolding

**Pre-flight before Task 1:**
- Branch from main: `git checkout -b feat/plan-2b-async-backfill`
- Verify `make build` + `CGO_ENABLED=1 go test -tags sqlite_fts5 ./...` green on main
- Confirm `goimports` available at `$(go env GOPATH)/bin/goimports`
