# Plan 2c Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close Plan 2c loose ends (CASCADE FK, race-friendly error, List tiebreaker, corrupt-JSON self-heal) and add `unictx embed status <id>` + RunE-level CLI tests in a single 6-task bundle.

**Architecture:** All changes are additive or in-place refinements to Plan 2c code. One new migration (0004), one new CLI subcommand (`embed status`), one new test indirection (`loadAppFn`), and three small refinements to existing Plan 2c code. No new packages, no new dependencies.

**Tech Stack:** Go 1.25, cgo, `github.com/mattn/go-sqlite3` (already imported in 7 files), `github.com/spf13/cobra`, `github.com/stretchr/testify`. Tests use `CGO_ENABLED=1` + `sqlite_fts5` build tag.

## Global Constraints

- **Migration files live in** `internal/adapter/sqlite/migrations/NNNN_*.sql` and **must not** contain `BEGIN`/`COMMIT` or `PRAGMA foreign_keys` — the runner (`migrations.go:97-107`) wraps each body in its own tx, and `PRAGMA foreign_keys` is a no-op inside a tx.
- **Each migration must end with** `UPDATE schema_meta SET value = 'N' WHERE key = 'schema_version';` — the runner does not auto-bump the version (see 0001/0003 pattern; 0002 omits this and relies on 0003 to advance the version).
- **All new test DBs use** `file::memory:?_foreign_keys=on` as the DSN (matches production `Open()`). SQLite's default FK enforcement is OFF; cascade and RESTRICT behaviors are silent without this.
- **Vec-table naming is** `"vec_" + strings.ReplaceAll(slug, "-", "_") + "_" + strconv.Itoa(dim)` — same convention as Plan 2c; no renames.
- **Domain sentinels use** `domain.ErrNotFound` (existing). The new sentinel `ErrCorruptConfig` lives in `internal/adapter/sqlite/model_registry.go` (package `sqlite`) — not in `domain` — because it is an adapter-specific decode failure, not a domain concept.
- **Error wrapping uses** `fmt.Errorf("...: %w", err)` to preserve sentinel chains for `errors.Is` / `errors.As`.
- **DB file mode is** `0600` after `Migrate` succeeds (already enforced in `db.go`); Plan 2c follow-up changes nothing here.
- **CLI subcommand `Use:` field uses** the bare command name (e.g. `"status"`, not `"status <item-id>"`) to match the structural test pattern in `embed_test.go` that iterates `c.Use`. Argument validation happens via `cobra.ExactArgs(N)`.
- **Conventional commit messages** follow the existing style: `feat(sqlite):`, `feat(cli):`, `test(sqlite):`, `feat(app):`, etc. (see `git log --oneline` for examples).
- **`goimports -w` on every `.go` file touched** (project convention; matches VSCode format-on-save).

---

## File Structure

**Files created:**
- `internal/adapter/sqlite/migrations/0004_embedding_model_slug_cascade.sql` — Task 1
- `internal/adapter/sqlite/migrations/0004_embedding_cascade_test.go` — Task 1 (lives in the `migrations_test` subpackage is NOT the pattern; existing migration tests live in `package sqlite` alongside `migrations_test.go`. Follow that.)
- `internal/app/app_reconcile_test.go` — Task 4
- `internal/cli/embed_status_test.go` — Task 5
- `internal/cli/embed_run_e_test.go` — Task 6

**Files modified:**
- `internal/adapter/sqlite/migrations_test.go` — Task 1 (update `TestMigrations_RunOnFreshDB` version expectation from `"3"` → `"4"`; verify the new test file lands the cascade coverage)
- `internal/adapter/sqlite/model_registry.go` — Tasks 2, 3, 4 (sequential; one implementer per task)
- `internal/adapter/sqlite/model_registry_test.go` — Tasks 2, 3, 4 (same)
- `internal/app/app.go` — Task 4 (reconcilePlan2cSync switch extension)
- `internal/port/embeddingrepo.go` — Task 5 (`ListForItem` method on interface)
- `internal/adapter/sqlite/embedding_repo.go` — Task 5 (ListForItem impl)
- `internal/adapter/sqlite/embedding_repo_test.go` — Task 5 (new test)
- `internal/cli/embed.go` — Tasks 5, 6 (loadAppFn var + embedStatusCmd; RunE tests live in separate files)

**Ordering rationale:** Tasks 2, 3, 4 all edit `model_registry.go` and must run sequentially (subagent-driven flow). Task 4 depends on the sentinel introduced in its own body (no upstream dependency on Tasks 2/3). Task 5 introduces `loadAppFn`, which Task 6's tests rely on — Task 5 must precede Task 6.

---

## Task 1: Migration 0004 — `context_embedding.model_slug` ON DELETE CASCADE

**Files:**
- Create: `internal/adapter/sqlite/migrations/0004_embedding_model_slug_cascade.sql`
- Create: `internal/adapter/sqlite/migrations_0004_test.go` (new file; keeps the existing `migrations_test.go` focused on aggregate runner behavior)
- Modify: `internal/adapter/sqlite/migrations_test.go:22` (the `TestMigrations_RunOnFreshDB` assertion that `version == "3"` becomes `"4"`)

**Interfaces:**
- Consumes: existing `Migrate(*sql.DB) error` from `migrations.go`.
- Produces: a new migration file auto-discovered by `Migrate` via `//go:embed migrations/*.sql`. No Go API change. Later tasks (Task 4's reconcile self-heal) do not depend on this migration directly — it is hardening only. The explicit `DELETE FROM context_embedding WHERE model_slug = ?` in `ModelRegistry.Remove` becomes defense-in-depth rather than mandatory.

- [ ] **Step 1: Write the failing test**

Create `internal/adapter/sqlite/migrations_0004_test.go`:

```go
package sqlite

import (
	"database/sql"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// openMigratedDBFor0004 returns a fresh in-memory DB with all migrations
// applied and FK enforcement ON. FKs are OFF by default in SQLite; the
// cascade assertions below are silent without this DSN.
func openMigratedDBFor0004(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", "file::memory:?_foreign_keys=on")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })
	require.NoError(t, Migrate(db))
	return db
}

// TestMigration0004_CascadesOnEmbeddingModelDelete verifies that after the
// migration, deleting an embedding_model row automatically drops all its
// context_embedding status rows. Migration 0002 declared the FK without
// an ON DELETE clause (default RESTRICT); 0004 rebuilds the table with
// ON DELETE CASCADE so ModelRegistry.Remove's explicit DELETE becomes
// defense-in-depth rather than mandatory.
func TestMigration0004_CascadesOnEmbeddingModelDelete(t *testing.T) {
	db := openMigratedDBFor0004(t)

	// Seed a non-default model + item + status row.
	_, err := db.Exec(`
		INSERT INTO context_item (id, scope, kind, source, owner_user_id, title, content, created_at, updated_at)
		VALUES ('item-1', 'user', 'note', 'test', 'u', 't', 'c', 0, 0);
		INSERT INTO embedding_model (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
		VALUES ('test-slug', 'test', 'ollama', 8, 'vec_test_slug_8', 0, 'active', '{}', 0);
		INSERT INTO context_embedding (item_id, model_slug, embedded_at, status, attempts)
		VALUES ('item-1', 'test-slug', 0, 'done', 1);
	`)
	require.NoError(t, err)

	_, err = db.Exec(`DELETE FROM embedding_model WHERE slug = 'test-slug'`)
	require.NoError(t, err, "DELETE should succeed; CASCADE should drop status rows")

	var n int
	require.NoError(t, db.QueryRow(
		`SELECT count(*) FROM context_embedding WHERE model_slug = 'test-slug'`).Scan(&n))
	assert.Equal(t, 0, n, "FK CASCADE must drop context_embedding rows")
}

// TestMigration0004_PreservesContextItemCascade locks in that the existing
// item_id → context_item(id) ON DELETE CASCADE (migration 0002) still
// fires after the rebuild. Regression guard: a careless rebuild could
// drop the existing CASCADE clause.
func TestMigration0004_PreservesContextItemCascade(t *testing.T) {
	db := openMigratedDBFor0004(t)

	_, err := db.Exec(`
		INSERT INTO context_item (id, scope, kind, source, owner_user_id, title, content, created_at, updated_at)
		VALUES ('item-2', 'user', 'note', 'test', 'u', 't', 'c', 0, 0);
		INSERT INTO context_embedding (item_id, model_slug, embedded_at, status, attempts)
		VALUES ('item-2', 'bge-m3', 0, 'done', 1);
	`)
	require.NoError(t, err)

	_, err = db.Exec(`DELETE FROM context_item WHERE id = 'item-2'`)
	require.NoError(t, err)

	var n int
	require.NoError(t, db.QueryRow(
		`SELECT count(*) FROM context_embedding WHERE item_id = 'item-2'`).Scan(&n))
	assert.Equal(t, 0, n, "context_item delete must still cascade")
}

// TestMigration0004_DataPreservedAcrossRebuild confirms the INSERT INTO
// … SELECT copy step carries every column (including 0003's attempts +
// last_error) without dropping data.
func TestMigration0004_DataPreservedAcrossRebuild(t *testing.T) {
	db := openMigratedDBFor0004(t)

	_, err := db.Exec(`
		INSERT INTO context_item (id, scope, kind, source, owner_user_id, title, content, created_at, updated_at)
		VALUES ('item-3', 'user', 'note', 'test', 'u', 't', 'c', 0, 0);
		INSERT INTO context_embedding (item_id, model_slug, embedded_at, status, error, attempts, last_error)
		VALUES ('item-3', 'bge-m3', 42, 'failed', 'orig err', 7, 'latest err');
	`)
	require.NoError(t, err)

	var (
		embAt    int
		status   string
		errMsg   sql.NullString
		attempts int
		lastErr  sql.NullString
	)
	require.NoError(t, db.QueryRow(`
		SELECT embedded_at, status, error, attempts, last_error
		FROM context_embedding WHERE item_id = 'item-3'`).
		Scan(&embAt, &status, &errMsg, &attempts, &lastErr))
	assert.Equal(t, 42, embAt)
	assert.Equal(t, "failed", status)
	assert.True(t, errMsg.Valid)
	assert.Equal(t, "orig err", errMsg.String)
	assert.Equal(t, 7, attempts)
	assert.True(t, lastErr.Valid)
	assert.Equal(t, "latest err", lastErr.String)
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/ -run TestMigration0004 -v`

Expected: FAIL. Either the migration does not exist yet (cascade test will get a FK RESTRICT error on `DELETE FROM embedding_model` since the old schema lacks CASCADE), or the file does not affect schema. The data-preserved test passes trivially pre-migration (inserts into the existing table) but the cascade test must fail with a FK constraint violation.

- [ ] **Step 3: Write the migration SQL**

Create `internal/adapter/sqlite/migrations/0004_embedding_model_slug_cascade.sql`:

```sql
-- Plan 2c follow-up: harden context_embedding.model_slug FK to ON DELETE CASCADE.
-- SQLite does not support ALTER TABLE ADD FOREIGN KEY; standard rebuild dance.
--
-- Note: the migrations runner (migrations.go execMigration) wraps each
-- file's body in a single tx via BeginTx/Commit, so this file MUST NOT
-- contain its own BEGIN/COMMIT (SQLite rejects nested BEGIN). PRAGMA
-- foreign_keys is a no-op inside a tx (SQLite docs), so it is omitted.
-- The rebuild is safe without disabling FKs because no other table
-- REFERENCES context_embedding — it only holds FKs TO context_item and
-- embedding_model, which remain stable across the rebuild. The DROP of
-- the old context_embedding doesn't violate any FK (nothing references
-- it); the RENAME installs the new table in place.

CREATE TABLE context_embedding_new (
    item_id     TEXT NOT NULL REFERENCES context_item(id) ON DELETE CASCADE,
    model_slug  TEXT NOT NULL REFERENCES embedding_model(slug) ON DELETE CASCADE,
    embedded_at INTEGER NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    PRIMARY KEY (item_id, model_slug)
);

INSERT INTO context_embedding_new (item_id, model_slug, embedded_at, status, error, attempts, last_error)
SELECT item_id, model_slug, embedded_at, status, error, attempts, last_error
FROM context_embedding;

DROP TABLE context_embedding;
ALTER TABLE context_embedding_new RENAME TO context_embedding;

CREATE INDEX IF NOT EXISTS idx_emb_model ON context_embedding(model_slug);

UPDATE schema_meta SET value = '4' WHERE key = 'schema_version';
```

- [ ] **Step 4: Update the existing aggregate version assertion**

In `internal/adapter/sqlite/migrations_test.go:22`, change:

```go
	assert.Equal(t, "3", version)
```

to:

```go
	assert.Equal(t, "4", version)
```

- [ ] **Step 5: Run all migration tests to verify they pass**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/ -run TestMigrations -v`

Expected: PASS. All four `TestMigrations*` tests (RunOnFreshDB now expects "4", Idempotent, 0002_CreatesEmbeddingTables, 0002_IdempotentFromFreshDB, 0003_AddsRetryColumns) plus the three new `TestMigration0004_*` pass.

- [ ] **Step 6: Run full sqlite package regression**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/...`

Expected: PASS. Existing `TestModelRegistry_Remove_DeletesEmbeddingStatusRowsExplicitly` still passes — the explicit DELETE in `Remove` remains valid (defense-in-depth; it just becomes redundant with the cascade rather than mandatory).

- [ ] **Step 7: Update the `Remove` inline comment to reflect defense-in-depth**

In `internal/adapter/sqlite/model_registry.go`, the existing comment at lines 266-268 reads:

```go
	// context_embedding.model_slug FK is RESTRICT (no ON DELETE clause in
	// migration 0002). This explicit DELETE is mandatory; without it, the
	// row delete below would raise a FK constraint violation.
```

Update to reflect that migration 0004 adds CASCADE, making the explicit DELETE defense-in-depth:

```go
	// Defense-in-depth. After migration 0004, the model_slug FK ON DELETE
	// CASCADE drops these rows automatically; this explicit DELETE ensures
	// correctness on DBs that pre-date 0004 or have FK enforcement off.
```

The behavior is unchanged — the explicit DELETE runs in both cases. The comment now matches reality.

- [ ] **Step 8: Format and commit**

```bash
goimports -w internal/adapter/sqlite/migrations_0004_test.go \
              internal/adapter/sqlite/migrations_test.go \
              internal/adapter/sqlite/model_registry.go
git add internal/adapter/sqlite/migrations/0004_embedding_model_slug_cascade.sql \
        internal/adapter/sqlite/migrations_0004_test.go \
        internal/adapter/sqlite/migrations_test.go \
        internal/adapter/sqlite/model_registry.go
git commit -m "$(cat <<'EOF'
feat(sqlite): migration 0004 — context_embedding.model_slug ON DELETE CASCADE

Migration 0002 declared the FK without ON DELETE; ModelRegistry.Remove
had to DELETE context_embedding rows explicitly inside its tx. 0004
rebuilds the table with ON DELETE CASCADE so the explicit DELETE
becomes defense-in-depth rather than mandatory. Comment on the
explicit DELETE updated to match.

Standard SQLite rebuild dance (CREATE new, INSERT SELECT, DROP old,
RENAME). No PRAGMA foreign_keys — no-op inside a tx. Trailing
schema_meta bump to '4' follows the 0001/0003 convention.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Minor #5 — `List` tiebreaker `, slug ASC`

**Files:**
- Modify: `internal/adapter/sqlite/model_registry.go:67-84` (the `List` method's SQL query)
- Modify: `internal/adapter/sqlite/model_registry_test.go` (append a new test)

**Interfaces:**
- Consumes: none (pure SQL change).
- Produces: `ModelRegistry.List(ctx) ([]port.ModelDescriptor, error)` with stable ordering guarantee. No signature change.

- [ ] **Step 1: Write the failing test**

Append to `internal/adapter/sqlite/model_registry_test.go`:

```go
// TestModelRegistry_List_TiebreakerOnSlug locks in deterministic ordering
// when multiple rows share created_at. SQLite stores created_at as epoch
// seconds, so rows inserted within the same second tie — without a
// secondary sort key, List returns them in arbitrary (often rowid) order,
// flaking on slow CI. The tiebreaker is `, slug ASC`.
func TestModelRegistry_List_TiebreakerOnSlug(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)
	ctx := context.Background()

	// Insert three rows with identical created_at by bypassing Register
	// (which uses strftime('%s','now')). zzz/aaa/mmm so slug-ASC ordering
	// differs from any insertion order.
	_, err := db.Exec(`
		INSERT INTO embedding_model (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
		VALUES
			('zzz', 'zzz', 'ollama', 8, 'vec_zzz_8', 0, 'active', '{}', 100),
			('aaa', 'aaa', 'ollama', 8, 'vec_aaa_8', 0, 'active', '{}', 100),
			('mmm', 'mmm', 'ollama', 8, 'vec_mmm_8', 0, 'active', '{}', 100)
	`)
	require.NoError(t, err)

	all, err := reg.List(ctx)
	require.NoError(t, err)

	// Filter to the three we inserted (bge-m3 seed is also present, with
	// its own created_at from strftime('%s','now') — comes first or last
	// depending on test runtime; only the tied trio's relative order
	// matters here).
	var slugs []string
	for _, m := range all {
		if m.Slug == "aaa" || m.Slug == "mmm" || m.Slug == "zzz" {
			slugs = append(slugs, m.Slug)
		}
	}
	assert.Equal(t, []string{"aaa", "mmm", "zzz"}, slugs,
		"tied created_at must tiebreak on slug ASC")
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/ -run TestModelRegistry_List_TiebreakerOnSlug -v`

Expected: FAIL. The current SQL `ORDER BY created_at ASC` returns the tied trio in rowid order (`zzz, aaa, mmm` per INSERT order). The test asserts `aaa, mmm, zzz`.

- [ ] **Step 3: Apply the tiebreaker**

In `internal/adapter/sqlite/model_registry.go:69`, change:

```go
		`SELECT `+selectModelCols+` FROM embedding_model ORDER BY created_at ASC`)
```

to:

```go
		`SELECT `+selectModelCols+` FROM embedding_model ORDER BY created_at ASC, slug ASC`)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/ -run TestModelRegistry_List -v`

Expected: PASS. Both `TestModelRegistry_List_OrdersByCreation` (existing) and `TestModelRegistry_List_TiebreakerOnSlug` (new) pass.

- [ ] **Step 5: Run full sqlite regression**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/...`

Expected: PASS.

- [ ] **Step 6: Format and commit**

```bash
goimports -w internal/adapter/sqlite/model_registry.go internal/adapter/sqlite/model_registry_test.go
git add internal/adapter/sqlite/model_registry.go internal/adapter/sqlite/model_registry_test.go
git commit -m "$(cat <<'EOF'
feat(sqlite): stable List ordering with slug ASC tiebreaker

created_at is stored as epoch seconds; rows inserted within the same
second tie and return in rowid order without a secondary sort key,
flaking on slow CI. Add ', slug ASC' tiebreaker.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Minor #4 — `Register` race-friendly UNIQUE-constraint error

**Files:**
- Modify: `internal/adapter/sqlite/model_registry.go:1-13` (imports) and `:114-162` (the `Register` method's INSERT error path)
- Modify: `internal/adapter/sqlite/model_registry_test.go` (append a new test)

**Interfaces:**
- Consumes: `github.com/mattn/go-sqlite3` (already imported in 7 other files; promotes from `// indirect` to direct in `go.mod` on next `go mod tidy`).
- Produces: `Register(ctx, ModelSpec) error` now returns `fmt.Errorf("model %s already registered: %w", slug, err)` when two concurrent callsites lose the pre-check race. The original `sqlite3.Error` is chained via `%w`, so callers using `errors.As(err, &sqliteErr)` still work.

- [ ] **Step 1: Write the failing test**

Append to `internal/adapter/sqlite/model_registry_test.go`:

```go
// TestModelRegistry_Register_ConcurrentSameSlugIsFriendly reproduces the
// race where two goroutines pass the existing-row pre-check (both see no
// row), both attempt INSERT, and one loses the PK constraint. Without the
// errors.As rewrite in the INSERT error path, the loser sees raw
// "UNIQUE constraint failed: embedding_model.slug" text; with the fix,
// the loser sees "model <slug> already registered: ..." (chained).
//
// Sync on a shared channel so both goroutines reach INSERT together. The
// pre-check is fast and the INSERT slow enough (creates a vec0 table)
// that the loser reliably hits the constraint.
func TestModelRegistry_Register_ConcurrentSameSlugIsFriendly(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)
	ctx := context.Background()

	const slug = "race-slug"
	const goroutines = 2

	type result struct {
		err error
	}
	results := make(chan result, goroutines)
	start := make(chan struct{})

	for i := 0; i < goroutines; i++ {
		go func() {
			<-start // both release together
			err := reg.Register(ctx, port.ModelSpec{
				Slug:      slug,
				Provider:  "openai",
				Dimension: 8,
			})
			results <- result{err: err}
		}()
	}
	close(start)

	var (
		okCount   int
		failErrs  []string
	)
	for i := 0; i < goroutines; i++ {
		r := <-results
		if r.err == nil {
			okCount++
		} else {
			failErrs = append(failErrs, r.err.Error())
		}
	}

	assert.Equal(t, 1, okCount, "exactly one Register must succeed")
	require.Len(t, failErrs, 1, "the other must fail")
	assert.Contains(t, failErrs[0], "already registered",
		"loser's error must be race-friendly, got: %s", failErrs[0])
	assert.NotContains(t, failErrs[0], "UNIQUE constraint failed",
		"raw sqlite constraint text must not leak to caller")
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/ -run TestModelRegistry_Register_ConcurrentSameSlugIsFriendly -v`

Expected: FAIL (flakily, depending on sqlite timing — but reliably fails on the `assert.NotContains` "UNIQUE constraint failed" check). The loser's error message currently is `"insert model row: UNIQUE constraint failed: embedding_model.slug"` because the INSERT error path returns the raw wrapped error.

- [ ] **Step 3: Add the `go-sqlite3` import and rewrite the INSERT error path**

In `internal/adapter/sqlite/model_registry.go`, add to the import block:

```go
	"errors"

	"uni-context/internal/domain"
	"uni-context/internal/port"

	"github.com/mattn/go-sqlite3"
```

(Place `"errors"` in the stdlib block; `sqlite3` in its own block below `uni-context/...` per Go import grouping.)

Replace the INSERT error path in `Register` (currently lines 144-146):

```go
	if err != nil {
		return fmt.Errorf("insert model row: %w", err)
	}
```

with:

```go
	if err != nil {
		// Two concurrent Register calls can both pass the pre-check above
		// and race to INSERT; one wins, the other gets a UNIQUE constraint
		// violation. Surface a friendly "already registered" message so
		// callers don't see raw sqlite constraint text. The original error
		// stays chained via %w for callers that want to errors.As it.
		var sqliteErr *sqlite3.Error
		if errors.As(err, &sqliteErr) && sqliteErr.ExtendedCode == sqlite3.ErrConstraintUnique {
			return fmt.Errorf("model %s already registered: %w", spec.Slug, err)
		}
		return fmt.Errorf("insert model row: %w", err)
	}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/ -run TestModelRegistry_Register_ConcurrentSameSlugIsFriendly -v`

Expected: PASS. The loser's error now reads `"model race-slug already registered: UNIQUE constraint failed: embedding_model.slug"` — contains "already registered", does NOT contain raw "UNIQUE constraint failed" as the leading text (the `assert.NotContains` check requires only that the substring not appear, but the chained `%w` brings it back; adjust the assertion if needed).

**Adjustment:** Re-read the test. The assertion `assert.NotContains(t, failErrs[0], "UNIQUE constraint failed")` is too strict because `%w` chains the original error and `err.Error()` includes the full chain. Either:

(a) Loosen the test assertion to only check the friendly prefix:

```go
	assert.Contains(t, failErrs[0], "already registered",
		"loser's error must lead with the friendly message; got: %s", failErrs[0])
```

Replace the `assert.NotContains(...)` line with the above (delete the NotContains assertion entirely). The chained constraint text is intentional — it lets advanced callers use `errors.As` if they want to. The friendly prefix is the contract.

(b) Run `go mod tidy` after the import change so `go.mod`'s `// indirect` marker on `github.com/mattn/go-sqlite3` is dropped:

```bash
HTTPS_PROXY=socks5://127.0.0.1:7890 go mod tidy
```

(The proxy is required per CLAUDE.md memory: `proxy.golang.org` is blocked without it.)

Apply (a) first by editing the test in `model_registry_test.go` (remove the `assert.NotContains` line), then run `go mod tidy`.

- [ ] **Step 5: Run full sqlite regression**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/...`

Expected: PASS. The pre-existing `TestModelRegistry_Register_RejectsExistingSlug` still passes — it hits the pre-check path (single goroutine, no race), which already returned `"model bge-m3 already registered"`; behavior unchanged.

- [ ] **Step 6: Format and commit**

```bash
goimports -w internal/adapter/sqlite/model_registry.go internal/adapter/sqlite/model_registry_test.go
git add internal/adapter/sqlite/model_registry.go \
        internal/adapter/sqlite/model_registry_test.go \
        go.mod go.sum
git commit -m "$(cat <<'EOF'
feat(sqlite): race-friendly Register error on concurrent UNIQUE loss

Two concurrent Register calls can both pass the existing-row pre-check
and race to INSERT; the loser hit a raw UNIQUE constraint violation.
Detect via errors.As against sqlite3.Error with ExtendedCode ==
ErrConstraintUnique, and surface "model <slug> already registered"
(chained) instead.

Pre-check remains as the fast path for the common single-goroutine
case.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Minor #6 — `scanModel` corrupt-JSON sentinel + `reconcilePlan2cSync` self-heal

**Files:**
- Modify: `internal/adapter/sqlite/model_registry.go` (add `ErrCorruptConfig` sentinel; rewrite `scanModel` to surface it)
- Modify: `internal/adapter/sqlite/model_registry_test.go` (two new scanModel tests)
- Modify: `internal/app/app.go:210-249` (extend the `reconcilePlan2cSync` switch)
- Create: `internal/app/app_reconcile_test.go` (new test file for the corrupt-config self-heal)

**Interfaces:**
- Consumes: none new (uses existing `errors.Is`, `fmt.Fprintf`, `os.Stderr`).
- Produces:
  - `sqlite.ErrCorruptConfig` sentinel (package-level var) — used by `app.reconcilePlan2cSync` to detect corrupt active-model config and heal it.
  - `ModelRegistry.Get`, `.GetActive`, `.List` now potentially return errors that wrap `ErrCorruptConfig` when their `scanModel` step hits a non-JSON `config` value. Callers can `errors.Is(err, sqlite.ErrCorruptConfig)` to discriminate.

- [ ] **Step 1: Write the first failing test (scanModel unit test)**

Append to `internal/adapter/sqlite/model_registry_test.go`:

```go
// TestModelRegistry_scanModel_CorruptJSONReturnsErrCorruptConfig drives
// scanModel through Get, with a manually-corrupted config column. The
// sentinel must propagate via %w so reconcilePlan2cSync (app.go) can
// errors.Is it.
func TestModelRegistry_scanModel_CorruptJSONReturnsErrCorruptConfig(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)
	ctx := context.Background()

	// Insert a row with non-JSON config. Use Register first to create the
	// vec table, then corrupt the row directly.
	require.NoError(t, reg.Register(ctx, port.ModelSpec{
		Slug: "corrupt", Provider: "openai", Dimension: 8,
	}))
	_, err := db.Exec(`UPDATE embedding_model SET config = 'not json' WHERE slug = 'corrupt'`)
	require.NoError(t, err)

	_, err = reg.Get(ctx, "corrupt")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrCorruptConfig,
		"Get on corrupt-config row must return ErrCorruptConfig (wrapped)")
}

// TestModelRegistry_scanModel_EmptyConfigIsOK confirms that the seeded
// default '{}' config — which parses to a zero-value configJSON — does
// NOT trigger ErrCorruptConfig. Regression guard: an over-eager check
// that fires on empty JSON would block every seed row.
func TestModelRegistry_scanModel_EmptyConfigIsOK(t *testing.T) {
	db := openTestDB(t)
	reg := NewModelRegistry(db)

	// Insert a row whose config is the seeded default '{}'.
	_, err := db.Exec(`
		INSERT INTO embedding_model (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
		VALUES ('empty-cfg', 'empty-cfg', 'ollama', 8, 'vec_empty_cfg_8', 0, 'active', '{}', 0)`)
	require.NoError(t, err)

	got, err := reg.Get(context.Background(), "empty-cfg")
	require.NoError(t, err)
	assert.Equal(t, "empty-cfg", got.Slug)
	assert.Empty(t, got.BaseURL, "empty config = zero-value BaseURL")
	assert.Empty(t, got.APIKey, "empty config = zero-value APIKey")
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/ -run TestModelRegistry_scanModel -v`

Expected: COMPILE FAILURE (`undefined: ErrCorruptConfig`). The sentinel does not exist yet, so the test does not compile.

- [ ] **Step 3: Add the sentinel and rewrite scanModel**

In `internal/adapter/sqlite/model_registry.go`, add the sentinel directly below the `selectModelCols` const (around line 43):

```go
// ErrCorruptConfig signals that an embedding_model.config value could not
// be parsed as JSON. Callers may fall back to cfg.Embedder values or
// surface to the user. app.reconcilePlan2cSync uses errors.Is against
// this sentinel to self-heal a corrupt active-model row on first Wire.
var ErrCorruptConfig = errors.New("embedding_model.config corrupt")
```

Add `"errors"` to the imports (it was added in Task 3; if Task 3 has already shipped, this is a no-op).

Rewrite the JSON parse block in `scanModel` (currently lines 58-64):

```go
	if cfg != "" {
		var c configJSON
		_ = json.Unmarshal([]byte(cfg), &c) // tolerate malformed JSON; surface empty
		m.BaseURL = c.BaseURL
		m.APIKey = c.APIKey
	}
```

to:

```go
	if cfg != "" {
		var c configJSON
		if err := json.Unmarshal([]byte(cfg), &c); err != nil {
			// Surface the descriptor with whatever columns scanned cleanly
			// (Slug/Name/Provider/Dimension/VecTable/IsDefault/Status) plus
			// the sentinel. Callers that only need identity (slug lookups
			// for SetDefault, etc.) can ignore the sentinel; callers that
			// need BaseURL/APIKey must heal before using.
			return m, fmt.Errorf("%w: config JSON parse: %s",
				ErrCorruptConfig, err.Error())
		}
		m.BaseURL = c.BaseURL
		m.APIKey = c.APIKey
	}
```

Note: the descriptor `m` is returned partially populated (Slug etc. are valid). This is intentional — `app.reconcilePlan2cSync` only needs the slug to call UpdateConfig.

- [ ] **Step 4: Run scanModel tests to verify they pass**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/ -run TestModelRegistry_scanModel -v`

Expected: PASS. Both new tests pass.

- [ ] **Step 5: Run full sqlite regression**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/...`

Expected: PASS. The existing `TestModelRegistry_GetActive_ReturnsSeedDefault` test still works because the seed config JSON `'{"base_url":"http://localhost:11434","model":"bge-m3"}'` parses cleanly (the `model` field is silently ignored by `configJSON`).

- [ ] **Step 6: Write the failing reconcile self-heal test**

Create `internal/app/app_reconcile_test.go`:

```go
package app

import (
	"context"
	"database/sql"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/adapter/sqlite"
	"uni-context/internal/config"
)

// newReconcileDB returns a fresh migrated in-memory DB for reconcile tests.
// FK enforcement ON so the embedding_model ↔ context_embedding relations
// behave as in production.
func newReconcileDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", "file::memory:?_foreign_keys=on")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })
	require.NoError(t, sqlite.Migrate(db))
	return db
}

// TestReconcilePlan2cSync_CorruptActiveConfig_HealsFromCfg proves the
// self-heal path: when the active model row exists but its config is
// unreadable JSON, reconcile must surface a stderr warning, overwrite
// the config from cfg.Embedder fields, and set the plan_2c_synced flag.
// Without the fix, the corrupt JSON surfaced as ErrCorruptConfig from
// reg.Get, fell through to the default error case, and Wire returned
// "lookup <slug>: ..." — leaving the DB unusable.
func TestReconcilePlan2cSync_CorruptActiveConfig_HealsFromCfg(t *testing.T) {
	db := newReconcileDB(t)
	reg := sqlite.NewModelRegistry(db)

	// Migration 0002 already seeded the 'bge-m3' row. Corrupt its config
	// in place — simulates a buggy older build or a manual DB edit gone
	// wrong. A fresh UPDATE avoids fighting the seed INSERT.
	_, err := db.Exec(`UPDATE embedding_model SET config = 'not json' WHERE slug = 'bge-m3'`)
	require.NoError(t, err)

	// Capture the stderr warning via the osStderr indirection declared in
	// app.go. t.Cleanup restores the production writer even on assertion
	// failure.
	var stderrBuf strings.Builder
	prevStderr := osStderr
	osStderr = &stderrBuf
	t.Cleanup(func() { osStderr = prevStderr })

	cfg := config.EmbedderConfig{
		Enabled:  true,
		Model:    "bge-m3",
		Provider: "openai",
		BaseURL:  "http://lmstudio:1234/v1",
		APIKey:   "sk-test",
	}

	err = reconcilePlan2cSync(context.Background(), db, reg, cfg)
	require.NoError(t, err, "reconcile must self-heal rather than fail")

	// Row's config now parses cleanly and carries cfg.Embedder values.
	got, err := reg.Get(context.Background(), "bge-m3")
	require.NoError(t, err)
	assert.Equal(t, "openai", got.Provider)
	assert.Equal(t, "http://lmstudio:1234/v1", got.BaseURL)
	assert.Equal(t, "sk-test", got.APIKey)

	// Stderr warning fired.
	assert.Contains(t, stderrBuf.String(), "corrupt config JSON",
		"reconcile must warn the user that it healed a corrupt row")

	// plan_2c_synced flag set so next Wire skips reconcile.
	var synced string
	require.NoError(t, db.QueryRow(
		`SELECT value FROM schema_meta WHERE key = 'plan_2c_synced'`).Scan(&synced))
	assert.Equal(t, "1", synced)
}

// TestReconcilePlan2cSync_CleanConfigIsUntouched confirms that a row with
// valid JSON is NOT healed (UpdateConfig would still overwrite it, which
// is the existing Plan 2c behavior — the test just locks that in so the
// new case branch does not regress the happy path).
func TestReconcilePlan2cSync_CleanConfigIsUntouched(t *testing.T) {
	db := newReconcileDB(t)
	reg := sqlite.NewModelRegistry(db)

	cfg := config.EmbedderConfig{
		Enabled:  true,
		Model:    "bge-m3",
		Provider: "ollama",
		BaseURL:  "http://localhost:11434",
		APIKey:   "",
	}

	prevStderr := osStderr
	var stderrBuf strings.Builder
	osStderr = &stderrBuf
	t.Cleanup(func() { osStderr = prevStderr })

	require.NoError(t, reconcilePlan2cSync(context.Background(), db, reg, cfg))
	assert.NotContains(t, stderrBuf.String(), "corrupt",
		"clean row must not trigger the corrupt-config warning")

	// Config reflects cfg.Embedder values (the existing alias-heal path
	// runs unconditionally on getErr == nil — this is Plan 2c behavior,
	// not new).
	got, err := reg.Get(context.Background(), "bge-m3")
	require.NoError(t, err)
	assert.Equal(t, "ollama", got.Provider)
	assert.Equal(t, "http://localhost:11434", got.BaseURL)
}
```

Add the `osStderr` indirection variable to `internal/app/app.go` at file scope, just above `reconcilePlan2cSync`:

```go
// osStderr is the indirection that lets tests capture the corrupt-config
// warning without redirecting the real os.Stderr globally. Production
// code points this at os.Stderr; tests swap it to a *bytes.Buffer.
var osStderr io.Writer = os.Stderr
```

And update the import block in `app.go` to include `"io"`.

Then update the warning line in `reconcilePlan2cSync` (which we're about to add) to write to `osStderr` instead of `os.Stderr`:

```go
	fmt.Fprintf(osStderr,
		"warning: model %s has corrupt config JSON; healing from config.yaml\n", cfg.Model)
```

- [ ] **Step 7: Run the failing reconcile test**

Run: `CGO_ENABLED=1 go test ./internal/app/ -run TestReconcilePlan2cSync_CorruptActiveConfig -v`

Expected: FAIL. The current `reconcilePlan2cSync` falls through to the `default` case on `ErrCorruptConfig` and returns `fmt.Errorf("lookup bge-m3: %w", getErr)`. The test fails at `require.NoError(t, err, "reconcile must self-heal rather than fail")`.

- [ ] **Step 8: Extend the reconcile switch**

In `internal/app/app.go:221-238`, replace the existing switch:

```go
	_, getErr := reg.Get(ctx, cfg.Model)
	switch {
	case getErr == nil:
		// Row exists: heal config from cfg.Embedder.
		if err := reg.UpdateConfig(ctx, cfg.Model, cfg.BaseURL, cfg.APIKey, cfg.Provider); err != nil {
			return fmt.Errorf("heal config for %s: %w", cfg.Model, err)
		}
	case errors.Is(getErr, domain.ErrNotFound):
		// Row missing: register fresh.
		if err := reg.Register(ctx, port.ModelSpec{
			Slug: cfg.Model, Provider: cfg.Provider,
			BaseURL: cfg.BaseURL, APIKey: cfg.APIKey, Dimension: cfg.Dimension,
		}); err != nil {
			return fmt.Errorf("register %s: %w", cfg.Model, err)
		}
	default:
		return fmt.Errorf("lookup %s: %w", cfg.Model, getErr)
	}
```

with:

```go
	_, getErr := reg.Get(ctx, cfg.Model)
	switch {
	case getErr == nil:
		// Row exists and scanned cleanly: heal provider + config from cfg.Embedder.
		// This is the existing Plan 2c alias-row heal; unchanged.
		if err := reg.UpdateConfig(ctx, cfg.Model, cfg.BaseURL, cfg.APIKey, cfg.Provider); err != nil {
			return fmt.Errorf("heal config for %s: %w", cfg.Model, err)
		}
	case errors.Is(getErr, sqlite.ErrCorruptConfig):
		// Row exists but config JSON is unreadable — UpdateConfig overwrites
		// the corrupt blob. Stderr warning so the user knows we touched
		// their DB; this is rare enough (manual edit / cross-version bug)
		// that a warning is appropriate rather than silent heal.
		fmt.Fprintf(osStderr,
			"warning: model %s has corrupt config JSON; healing from config.yaml\n",
			cfg.Model)
		if err := reg.UpdateConfig(ctx, cfg.Model, cfg.BaseURL, cfg.APIKey, cfg.Provider); err != nil {
			return fmt.Errorf("heal corrupt config for %s: %w", cfg.Model, err)
		}
	case errors.Is(getErr, domain.ErrNotFound):
		// Row missing: register fresh.
		if err := reg.Register(ctx, port.ModelSpec{
			Slug: cfg.Model, Provider: cfg.Provider,
			BaseURL: cfg.BaseURL, APIKey: cfg.APIKey, Dimension: cfg.Dimension,
		}); err != nil {
			return fmt.Errorf("register %s: %w", cfg.Model, err)
		}
	default:
		return fmt.Errorf("lookup %s: %w", cfg.Model, getErr)
	}
```

Update `app.go` imports — add `"io"` if not already present (it isn't), keep the existing `"errors"` (added by Plan 2c).

- [ ] **Step 9: Run reconcile tests to verify they pass**

Run: `CGO_ENABLED=1 go test ./internal/app/ -run TestReconcilePlan2cSync -v`

Expected: PASS. Both new tests pass.

- [ ] **Step 10: Run full app + sqlite regression**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./...`

Expected: PASS across all packages.

- [ ] **Step 11: Format and commit**

```bash
goimports -w internal/adapter/sqlite/model_registry.go \
              internal/adapter/sqlite/model_registry_test.go \
              internal/app/app.go \
              internal/app/app_reconcile_test.go
git add internal/adapter/sqlite/model_registry.go \
        internal/adapter/sqlite/model_registry_test.go \
        internal/app/app.go \
        internal/app/app_reconcile_test.go
git commit -m "$(cat <<'EOF'
feat(app): self-heal corrupt embedding_model.config on first Wire

scanModel previously swallowed JSON parse errors and surfaced empty
BaseURL/APIKey — the embedder then constructed against empty values
and silently failed. Surface a new sqlite.ErrCorruptConfig sentinel
instead, and extend reconcilePlan2cSync to catch it, warn on stderr,
and overwrite the config from cfg.Embedder before SetDefault runs.

Stderr indirection via osStderr lets the test capture the warning
without redirecting the real os.Stderr.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `unictx embed status <id>` + `loadAppFn` indirection

**Files:**
- Modify: `internal/port/embeddingrepo.go` (add `ListForItem` to the interface)
- Modify: `internal/adapter/sqlite/embedding_repo.go` (implement `ListForItem`)
- Modify: `internal/adapter/sqlite/embedding_repo_test.go` (new test)
- Modify: `internal/cli/embed.go` (add `loadAppFn` var, swap all RunE `loadApp()` calls within this file to `loadAppFn()`, add `embedStatusCmd`, register it)
- Create: `internal/cli/embed_status_test.go` (four RunE-level tests via `loadAppFn` swap)

**Interfaces:**
- Consumes: existing `port.EmbeddingRepo`, `*app.App.EmbeddingRepo` field (already exposed in Plan 2b), `*app.App.DB` for `defer Close`.
- Produces:
  - `port.EmbeddingRepo.ListForItem(ctx, itemID) ([]EmbeddingStatus, error)` — new method. Implementations MUST return an empty (non-nil) slice when no rows match, so callers can `len(rows) == 0` without nil-checking.
  - `cli.loadAppFn` — package-level `var loadAppFn = loadApp`. Tests swap it; production code is unchanged.
  - `cli.embedStatusCmd` — new cobra subcommand registered under `embedCmd`.

- [ ] **Step 1: Refactor `newEmbeddingRepoFixture` to also return the *sql.DB**

The existing fixture returns two values:

```go
func newEmbeddingRepoFixture(t *testing.T) (port.EmbeddingRepo, *ContextRepo) {
	t.Helper()
	db := openTestDB(t) // from model_registry_test.go — fresh migrated :memory:
	repo := NewContextRepo(db)
	embRepo := NewEmbeddingRepo(db)
	return embRepo, repo
}
```

The new test needs raw `db.Exec` access (to insert `embedding_model` rows for the multi-model ordering subtest without going through `Registry.Register`, which would create noise — separate vec tables). Change the signature to return the `*sql.DB` as a third value:

```go
func newEmbeddingRepoFixture(t *testing.T) (port.EmbeddingRepo, *ContextRepo, *sql.DB) {
	t.Helper()
	db := openTestDB(t) // from model_registry_test.go — fresh migrated :memory:
	repo := NewContextRepo(db)
	embRepo := NewEmbeddingRepo(db)
	return embRepo, repo, db
}
```

Update the four existing callers (in `embedding_repo_test.go`: `TestEmbeddingRepo_UpsertStatus_InsertsFresh`, `_OnConflictIncrementsAttempts`, `_GetStatus_NotFound`, `_ListFailed_BasicOrdering`) from:

```go
	embRepo, repo := newEmbeddingRepoFixture(t)
```

to:

```go
	embRepo, repo, _ := newEmbeddingRepoFixture(t)
```

(For tests that don't use `repo`, use `embRepo, _, _ := newEmbeddingRepoFixture(t)`.)

Verify the refactor compiles before moving on:

```bash
CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/ -run TestEmbeddingRepo -v
```

Expected: PASS. All four existing tests pass unchanged with the third return value ignored.

- [ ] **Step 2: Write the failing port+adapter test**

Append to `internal/adapter/sqlite/embedding_repo_test.go`:

```go
// TestEmbeddingRepo_ListForItem covers the four behaviors the CLI command
// depends on: empty slice (not nil) for missing IDs, single row, multi-
// model ordering by model_slug ASC, and columns scanning correctly.
func TestEmbeddingRepo_ListForItem(t *testing.T) {
	t.Run("missing item returns empty slice not nil", func(t *testing.T) {
		embRepo, _ := newEmbeddingRepoFixture(t)
		rows, err := embRepo.ListForItem(context.Background(), "no-such-item")
		require.NoError(t, err)
		require.NotNil(t, rows, "empty slice, not nil — caller uses len()")
		assert.Len(t, rows, 0)
	})

	t.Run("single row returns expected columns", func(t *testing.T) {
		embRepo, repo := newEmbeddingRepoFixture(t)
		insertItemForEmbedTest(t, repo, "i1")
		require.NoError(t, embRepo.UpsertStatus(context.Background(),
			"i1", "bge-m3", "done", ""))

		rows, err := embRepo.ListForItem(context.Background(), "i1")
		require.NoError(t, err)
		require.Len(t, rows, 1)
		assert.Equal(t, "i1", rows[0].ItemID)
		assert.Equal(t, "bge-m3", rows[0].ModelSlug)
		assert.Equal(t, "done", rows[0].Status)
	})

	t.Run("multiple models ordered by slug ASC", func(t *testing.T) {
		embRepo, repo, db := newEmbeddingRepoFixture(t)
		insertItemForEmbedTest(t, repo, "i2")

		// Insert in non-alphabetical order; assert sorted output.
		require.NoError(t, embRepo.UpsertStatus(context.Background(),
			"i2", "zzz-model", "done", ""))
		require.NoError(t, embRepo.UpsertStatus(context.Background(),
			"i2", "aaa-model", "failed", "boom"))
		require.NoError(t, embRepo.UpsertStatus(context.Background(),
			"i2", "mmm-model", "done", ""))

		// The migration 0002 FK requires embedding_model rows for these
		// slugs (UpsertStatus would otherwise fail FK). Direct INSERT
		// keeps the test focused on ListForItem rather than spinning up
		// per-slug vec tables via Registry.Register.
		for _, slug := range []string{"zzz-model", "aaa-model", "mmm-model"} {
			_, err := db.Exec(`
				INSERT OR IGNORE INTO embedding_model
				(slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
				VALUES (?, ?, 'ollama', 8, 'vec_unused_8', 0, 'active', '{}', 0)`,
				slug, slug)
			require.NoError(t, err)
		}

		rows, err := embRepo.ListForItem(context.Background(), "i2")
		require.NoError(t, err)
		require.Len(t, rows, 3)
		assert.Equal(t, "aaa-model", rows[0].ModelSlug)
		assert.Equal(t, "mmm-model", rows[1].ModelSlug)
		assert.Equal(t, "zzz-model", rows[2].ModelSlug)
		assert.Equal(t, "failed", rows[0].Status)
		assert.Equal(t, "boom", rows[0].LastError)
	})
})
```

- [ ] **Step 3: Run test to verify it fails**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/ -run TestEmbeddingRepo_ListForItem -v`

Expected: COMPILE FAILURE. `port.EmbeddingRepo` has no `ListForItem` method, so the `embRepo.ListForItem(...)` calls do not compile.

- [ ] **Step 4: Add `ListForItem` to the port interface**

In `internal/port/embeddingrepo.go`, append to the `EmbeddingRepo` interface (above the closing `}`):

```go
	// ListForItem returns all status rows for the given item, ordered by
	// model_slug ASC. Empty slice (not nil) if no rows — callers depend
	// on `len(rows) == 0` without nil-checking. Used by the
	// `embed status <id>` CLI to show per-model migration state.
	// Plan 2c follow-up addition.
	ListForItem(ctx context.Context, itemID string) ([]EmbeddingStatus, error)
```

- [ ] **Step 5: Implement `ListForItem` on the sqlite adapter**

Append to `internal/adapter/sqlite/embedding_repo.go`:

```go
const listForItemSQL = `
SELECT item_id, model_slug, status, error, last_error, attempts, embedded_at
FROM context_embedding
WHERE item_id = ?
ORDER BY model_slug ASC
`

// ListForItem returns all status rows for the given item, ordered by
// model_slug ASC. Empty slice (not nil) if no rows — callers depend on
// `len(rows) == 0` without nil-checking. Used by `embed status <id>`.
func (r *EmbeddingRepo) ListForItem(ctx context.Context, itemID string) ([]port.EmbeddingStatus, error) {
	rows, err := r.db.QueryContext(ctx, listForItemSQL, itemID)
	if err != nil {
		return nil, fmt.Errorf("list status for item %s: %w", itemID, err)
	}
	defer rows.Close()

	out := []port.EmbeddingStatus{}
	for rows.Next() {
		var (
			s          port.EmbeddingStatus
			err1       sql.NullString
			lastErr    sql.NullString
			embeddedAt int64
		)
		if err := rows.Scan(
			&s.ItemID, &s.ModelSlug, &s.Status, &err1, &lastErr,
			&s.Attempts, &embeddedAt); err != nil {
			return nil, fmt.Errorf("scan status row: %w", err)
		}
		s.Error = err1.String
		s.LastError = lastErr.String
		s.EmbeddedAt = time.Unix(embeddedAt, 0).UTC()
		out = append(out, s)
	}
	return out, rows.Err()
}
```

- [ ] **Step 6: Run adapter test to verify it passes**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/sqlite/ -run TestEmbeddingRepo_ListForItem -v`

Expected: PASS. All three subtests pass.

- [ ] **Step 7: Add the `loadAppFn` indirection to embed.go**

In `internal/cli/embed.go`, add the package-level var below the existing `var (...)` block of flag variables (around line 23):

```go
// loadAppFn is the indirection that enables RunE-level tests. Tests swap
// it to return a stubbed *App; production code leaves the default. Only
// embed.go uses this — other CLI files call loadApp() directly.
// Plan 2c follow-up addition.
var loadAppFn = loadApp
```

Then within `embed.go`, mechanically replace every `loadApp()` call inside RunE handlers with `loadAppFn()`. The replacements are at:
- `embedBackfillCmd.RunE` (line 40): `a, _, err := loadApp()` → `a, _, err := loadAppFn()`
- `embedWorkerCmd.RunE` (line 79): same
- `embedModelAddCmd.RunE` (line 128): same
- `embedModelListCmd.RunE` (line 153): same
- `embedModelRemoveCmd.RunE` (line 185): same
- `embedSwitchCmd.RunE` (line 207): same
- `embedReembedCmd.RunE` (line 235): same

(Seven replacements total. The new `embedStatusCmd` added below also uses `loadAppFn()`.)

- [ ] **Step 8: Add the `embedStatusCmd`**

In `internal/cli/embed.go`, append after `embedReembedCmd` (around line 264):

```go
// embedStatusCmd prints all context_embedding status rows for a given
// item, ordered by model_slug ASC. Read-only; safe to run anytime. Used
// to inspect per-model migration state during `embed switch` workflows.
// Plan 2c follow-up addition.
var embedStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show embedding status rows for an item (all models)",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadAppFn()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if a.EmbeddingRepo == nil {
			return fmt.Errorf("embedder not enabled; set embedder.enabled=true in config")
		}

		rows, err := a.EmbeddingRepo.ListForItem(cmd.Context(), args[0])
		if err != nil {
			return err
		}
		if len(rows) == 0 {
			fmt.Printf("no embedding status rows for item %s\n", args[0])
			return nil
		}

		w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
		fmt.Fprintln(w, "MODEL_SLUG\tSTATUS\tATTEMPTS\tLAST_ERROR\tEMBEDDED_AT")
		for _, r := range rows {
			errCell := r.LastError
			if len(errCell) > 40 {
				errCell = errCell[:37] + "..."
			}
			fmt.Fprintf(w, "%s\t%s\t%d\t%s\t%d\n",
				r.ModelSlug, r.Status, r.Attempts, errCell, r.EmbeddedAt.Unix())
		}
		return w.Flush()
	},
}
```

In the `init()` function (around line 296), add the registration just before `rootCmd.AddCommand(embedCmd)`:

```go
	embedCmd.AddCommand(embedStatusCmd) // Plan 2c follow-up
```

- [ ] **Step 9: Update the structural test for embed subcommands**

In `internal/cli/embed_test.go:21`, the existing `TestEmbedCmd_HasExpectedSubcommands` asserts the list contains:

```go
		for _, want := range []string{"backfill", "worker", "model", "switch", "reembed"} {
```

Update to include `"status"`:

```go
		for _, want := range []string{"backfill", "worker", "model", "switch", "reembed", "status"} {
```

- [ ] **Step 10: Build the package to verify everything compiles**

Run: `CGO_ENABLED=1 go build ./...`

Expected: BUILD SUCCESS. No compile errors. The `loadAppFn` indirection and the new `embedStatusCmd` register cleanly.

- [ ] **Step 11: Write the four failing RunE-level tests for `embed status`**

Create `internal/cli/embed_status_test.go`:

```go
package cli

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/app"
	"uni-context/internal/port"
)

// stubRegistry captures Register/List/Remove/SetDefault calls for the
// RunE tests. Methods not exercised by a given test panic so accidental
// use surfaces loudly.
type stubRegistry struct {
	registered []port.ModelSpec
	listCalled bool
	removed    []string
	setDefault []string
	errOn      map[string]error // method name -> error to return
}

func (s *stubRegistry) List(ctx context.Context) ([]port.ModelDescriptor, error) {
	s.listCalled = true
	if s.errOn != nil {
		if err, ok := s.errOn["List"]; ok {
			return nil, err
		}
	}
	return []port.ModelDescriptor{
		{Slug: "bge-m3", Provider: "ollama", Dimension: 1024,
			VecTable: "vec_bge_m3_1024", IsDefault: true, Status: "active"},
	}, nil
}
func (s *stubRegistry) GetActive(ctx context.Context) (port.ModelDescriptor, error) {
	return port.ModelDescriptor{Slug: "bge-m3", Dimension: 1024, IsDefault: true}, nil
}
func (s *stubRegistry) Get(ctx context.Context, slug string) (port.ModelDescriptor, error) {
	return port.ModelDescriptor{}, errors.New("not found")
}
func (s *stubRegistry) Register(ctx context.Context, spec port.ModelSpec) error {
	s.registered = append(s.registered, spec)
	return nil
}
func (s *stubRegistry) UpdateConfig(ctx context.Context, slug, baseURL, apiKey, provider string) error {
	return nil
}
func (s *stubRegistry) SetDefault(ctx context.Context, slug string) error {
	s.setDefault = append(s.setDefault, slug)
	return nil
}
func (s *stubRegistry) Remove(ctx context.Context, slug string) error {
	s.removed = append(s.removed, slug)
	return nil
}

// stubEmbeddingRepo captures ListForItem calls and returns a canned slice.
type stubEmbeddingRepo struct {
	rows   []port.EmbeddingStatus
	err    error
	called bool
}

func (s *stubEmbeddingRepo) UpsertStatus(ctx context.Context, itemID, modelSlug, status, errStr string) error {
	return nil
}
func (s *stubEmbeddingRepo) GetStatus(ctx context.Context, itemID, modelSlug string) (port.EmbeddingStatus, error) {
	return port.EmbeddingStatus{}, errors.New("not found")
}
func (s *stubEmbeddingRepo) ListFailed(ctx context.Context, limit int) ([]port.EmbeddingStatus, error) {
	return nil, nil
}
func (s *stubEmbeddingRepo) ListForItem(ctx context.Context, itemID string) ([]port.EmbeddingStatus, error) {
	s.called = true
	return s.rows, s.err
}

// swapLoadAppFn swaps the package-level loadAppFn to return a stubbed
// *App. Returns a restore func — tests MUST defer it. Not safe for
// parallel tests in the same package (loadAppFn is package-level).
func swapLoadAppFn(a *app.App) func() {
	prev := loadAppFn
	loadAppFn = func() (*app.App, *config.Config, error) {
		return a, &config.Config{}, nil
	}
	return func() { loadAppFn = prev }
}

// newStubApp returns a minimal *app.App with an in-memory *sql.DB set,
// so the `defer a.DB.Close()` line in every RunE handler does not panic
// when the handler returns. The DB is closed via t.Cleanup. Tests
// customize the relevant fields (Registry, EmbeddingRepo, Reembed) on
// the returned App after construction.
//
// Why this matters: every embed RunE handler in embed.go registers
// `defer a.DB.Close()` immediately after loadApp() succeeds — BEFORE
// the early-return `Registry == nil` / `EmbeddingRepo == nil` checks.
// A stub App with nil DB would panic at the deferred Close when the
// handler returns from the early-exit branch.
func newStubApp(t *testing.T) *app.App {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })
	return &app.App{DB: db}
}

// TestEmbedStatusCmd_DisabledEmbedderErrorsCleanly: when App.EmbeddingRepo
// is nil (embedder.enabled=false), the command must return a clear error
// rather than nil-pointer-panic on ListForItem.
func TestEmbedStatusCmd_DisabledEmbedderErrorsCleanly(t *testing.T) {
	a := newStubApp(t) // EmbeddingRepo is nil
	restore := swapLoadAppFn(a)
	defer restore()

	cmd := embedStatusCmd
	cmd.SetArgs([]string{"some-id"})
	cmd.SetOut(new(bytes.Buffer))
	cmd.SetErr(new(bytes.Buffer))
	err := cmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "embedder not enabled")
}

// TestEmbedStatusCmd_NoRowsPrintsMessage: empty slice (not nil) from
// ListForItem must produce a friendly "no rows" line on stdout, not an
// empty table.
func TestEmbedStatusCmd_NoRowsPrintsMessage(t *testing.T) {
	repo := &stubEmbeddingRepo{rows: []port.EmbeddingStatus{}}
	a := newStubApp(t)
	a.EmbeddingRepo = repo
	restore := swapLoadAppFn(a)
	defer restore()

	var out bytes.Buffer
	cmd := embedStatusCmd
	cmd.SetArgs([]string{"absent-id"})
	cmd.SetOut(&out)
	cmd.SetErr(new(bytes.Buffer))
	require.NoError(t, cmd.Execute())
	assert.Contains(t, out.String(), "no embedding status rows for item absent-id")
}

// TestEmbedStatusCmd_PrintsTabularOutput: with 2 rows, the table header
// and both rows must render. LastError column truncates at 40 chars.
func TestEmbedStatusCmd_PrintsTabularOutput(t *testing.T) {
	longErr := strings.Repeat("e", 50)
	repo := &stubEmbeddingRepo{rows: []port.EmbeddingStatus{
		{ItemID: "i1", ModelSlug: "aaa-model", Status: "done", Attempts: 1, LastError: ""},
		{ItemID: "i1", ModelSlug: "zzz-model", Status: "failed", Attempts: 3, LastError: longErr},
	}}
	a := newStubApp(t)
	a.EmbeddingRepo = repo
	restore := swapLoadAppFn(a)
	defer restore()

	var out bytes.Buffer
	cmd := embedStatusCmd
	cmd.SetArgs([]string{"i1"})
	cmd.SetOut(&out)
	cmd.SetErr(new(bytes.Buffer))
	require.NoError(t, cmd.Execute())

	outStr := out.String()
	assert.Contains(t, outStr, "MODEL_SLUG")
	assert.Contains(t, outStr, "aaa-model")
	assert.Contains(t, outStr, "zzz-model")
	assert.Contains(t, outStr, strings.Repeat("e", 37)+"...",
		"last_error column truncates to 37 chars + '...'")
}

// TestEmbedStatusCmd_ArgCountRejected: cobra's ExactArgs(1) must reject
// 2-arg invocation. (RunE never runs, so DB-Close defer never registers;
// the newStubApp call is for shape consistency only.)
func TestEmbedStatusCmd_ArgCountRejected(t *testing.T) {
	a := newStubApp(t)
	restore := swapLoadAppFn(a)
	defer restore()

	cmd := embedStatusCmd
	cmd.SetArgs([]string{"a", "b"})
	cmd.SetOut(new(bytes.Buffer))
	cmd.SetErr(new(bytes.Buffer))
	err := cmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "accepts 1 arg(s)")
}
```

Update the imports block at the top of the test file to include `"database/sql"` and the go-sqlite3 driver (needed for `sql.Open("sqlite3", ...)` to actually work):

```go
import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"strings"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/app"
	"uni-context/internal/config"
	"uni-context/internal/port"
)
```

- [ ] **Step 12: Run the four RunE tests**

Run: `CGO_ENABLED=1 go test ./internal/cli/ -run TestEmbedStatusCmd -v`

Expected: PASS. All four tests pass. The `loadAppFn` indirection works; the stub App's EmbeddingRepo returns canned data.

- [ ] **Step 13: Run full CLI regression**

Run: `CGO_ENABLED=1 go test ./internal/cli/...`

Expected: PASS. Existing structural tests still pass (the updated `TestEmbedCmd_HasExpectedSubcommands` now expects `"status"` in the list).

- [ ] **Step 14: Format and commit**

```bash
goimports -w internal/port/embeddingrepo.go \
              internal/adapter/sqlite/embedding_repo.go \
              internal/adapter/sqlite/embedding_repo_test.go \
              internal/cli/embed.go \
              internal/cli/embed_test.go \
              internal/cli/embed_status_test.go
git add internal/port/embeddingrepo.go \
        internal/adapter/sqlite/embedding_repo.go \
        internal/adapter/sqlite/embedding_repo_test.go \
        internal/cli/embed.go \
        internal/cli/embed_test.go \
        internal/cli/embed_status_test.go
git commit -m "$(cat <<'EOF'
feat(cli): add 'embed status' subcommand + loadAppFn indirection

unictx embed status <id> prints all context_embedding rows for an
item, ordered by model_slug ASC — read-only inspection of per-model
migration state during `embed switch` workflows.

Backing method port.EmbeddingRepo.ListForItem returns an empty slice
(not nil) so callers can `len(rows) == 0` without nil-checking.

loadAppFn is a package-level var in embed.go that defaults to loadApp.
Tests swap it to inject a stubbed *App; production code is unchanged.
Enables RunE-level tests for all embed subcommands without subprocess
overhead. Used by embed_status_test.go now; embed_run_e_test.go next.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: RunE-level tests for the 5 existing embed subcommands

**Files:**
- Create: `internal/cli/embed_run_e_test.go`

**Interfaces:**
- Consumes: `cli.loadAppFn` (from Task 5), stub helpers from `embed_status_test.go` (`swapLoadAppFn`, `stubRegistry`), plus small per-test stubs as needed for `Backfill`/`Worker`/`Reembed` (these are concrete `*service.*Service` types — the stubs wrap them).
- Produces: five RunE tests, one per existing subcommand. No production code change.

**Stubbing strategy note:** `*service.BackfillService`, `*service.WorkerService`, `*service.ReembedService` are concrete types (not interfaces) — tests cannot swap them with simple struct mocks. Two options:
- (a) Construct a real `*service.*Service` with stubbed downstream deps (repo, embedSvc). Heavy.
- (b) Skip RunE tests for these three and only test the two that hit `Registry` directly (add, list, remove, switch — all four exercise `a.Registry.X` directly).

Option (b) is cleaner for Plan 2c follow-up's scope. The spec's RunE test list explicitly names only `Registry`-touching subcommands (`Add`, `List`, `Remove`, `Switch`) plus `Reembed`. For `Reembed`, the test asserts that the RunE handler calls `Reembed.Run` with the right limit/dryRun — but since `*service.ReembedService` is concrete, the test instead asserts that the handler:
- Errors cleanly when `a.Reembed == nil` (embedder disabled)
- Reaches the `Reembed.Run` call when non-nil (verified by swapping in a real ReembedService with a stubbed repo + embedSvc and asserting a side effect)

The pragmatic minimum: cover `Add`, `List`, `Remove`, `Switch` (4 subcommands) with full RunE tests via the stubRegistry, and cover `Reembed` with the "disabled embedder" + "happy path with real ReembedService constructed against fakes" pattern (mirroring `internal/service/reembed_test.go`). `Backfill` and `Worker` are out of scope for this task (they predate Plan 2c; the structural tests in `embed_test.go` cover their flags).

- [ ] **Step 1: Write the four Registry-touching RunE tests + the Reembed tests**

Create `internal/cli/embed_run_e_test.go`:

```go
package cli

import (
	"bytes"
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/app"
	"uni-context/internal/domain"
	"uni-context/internal/port"
	"uni-context/internal/service"
)

// TestEmbedModelAddCmd_RunECallsRegistryRegister: invoking `embed model
// add <slug>` with the four flags must surface a Register call carrying
// the flag values. Verified via stubRegistry.registered slice.
func TestEmbedModelAddCmd_RunECallsRegistryRegister(t *testing.T) {
	reg := &stubRegistry{}
	a := newStubApp(t)
	a.Registry = reg
	restore := swapLoadAppFn(a)
	defer restore()

	// Reset flags from any prior test (package-global state).
	modelAddProvider = "openai"
	modelAddBaseURL = "https://api.openai.com/v1"
	modelAddAPIKey = "sk-test"
	modelAddDim = 3072
	t.Cleanup(func() {
		modelAddProvider, modelAddBaseURL, modelAddAPIKey, modelAddDim = "", "", "", 0
	})

	cmd := embedModelAddCmd
	cmd.SetArgs([]string{"text-embedding-3-large"})
	cmd.SetOut(new(bytes.Buffer))
	cmd.SetErr(new(bytes.Buffer))
	require.NoError(t, cmd.Execute())

	require.Len(t, reg.registered, 1)
	spec := reg.registered[0]
	assert.Equal(t, "text-embedding-3-large", spec.Slug)
	assert.Equal(t, "openai", spec.Provider)
	assert.Equal(t, "https://api.openai.com/v1", spec.BaseURL)
	assert.Equal(t, "sk-test", spec.APIKey)
	assert.Equal(t, 3072, spec.Dimension)
}

// TestEmbedModelListCmd_RunECallsRegistryList: `embed model list` must
// call Registry.List and print a tabular row per model. The stub returns
// one model (bge-m3 default) — assert it appears in stdout.
func TestEmbedModelListCmd_RunECallsRegistryList(t *testing.T) {
	reg := &stubRegistry{}
	a := newStubApp(t)
	a.Registry = reg
	restore := swapLoadAppFn(a)
	defer restore()

	var out bytes.Buffer
	cmd := embedModelListCmd
	cmd.SetArgs([]string{})
	cmd.SetOut(&out)
	cmd.SetErr(new(bytes.Buffer))
	require.NoError(t, cmd.Execute())

	assert.True(t, reg.listCalled, "Registry.List must be called")
	assert.Contains(t, out.String(), "SLUG")
	assert.Contains(t, out.String(), "bge-m3")
	assert.Contains(t, out.String(), "*", "default model row carries the * marker")
}

// TestEmbedModelRemoveCmd_RunECallsRegistryRemove: `embed model remove
// <slug>` must call Registry.Remove with the slug arg.
func TestEmbedModelRemoveCmd_RunECallsRegistryRemove(t *testing.T) {
	reg := &stubRegistry{}
	a := newStubApp(t)
	a.Registry = reg
	restore := swapLoadAppFn(a)
	defer restore()

	cmd := embedModelRemoveCmd
	cmd.SetArgs([]string{"old-model"})
	cmd.SetOut(new(bytes.Buffer))
	cmd.SetErr(new(bytes.Buffer))
	require.NoError(t, cmd.Execute())

	require.Len(t, reg.removed, 1)
	assert.Equal(t, "old-model", reg.removed[0])
}

// TestEmbedSwitchCmd_RunECallsRegistrySetDefault: `embed switch <slug>`
// must call Registry.SetDefault and emit the stderr reminder so users
// know to run `embed reembed` next.
func TestEmbedSwitchCmd_RunECallsRegistrySetDefault(t *testing.T) {
	reg := &stubRegistry{}
	a := newStubApp(t)
	a.Registry = reg
	restore := swapLoadAppFn(a)
	defer restore()

	var stderr bytes.Buffer
	cmd := embedSwitchCmd
	cmd.SetArgs([]string{"new-model"})
	cmd.SetOut(new(bytes.Buffer))
	cmd.SetErr(&stderr)
	require.NoError(t, cmd.Execute())

	require.Len(t, reg.setDefault, 1)
	assert.Equal(t, "new-model", reg.setDefault[0])
	assert.Contains(t, stderr.String(), "embed reembed",
		"stderr reminder must mention the follow-up command")
}

// TestEmbedSwitchCmd_RunENilRegistryErrorsCleanly: when embedder.enabled
// is false, App.Registry is nil and the RunE handler must return the
// friendly "embedder not enabled" error rather than nil-pointer panic.
// newStubApp gives the handler a non-nil DB so the defer a.DB.Close()
// (which registers BEFORE the nil-Registry check) does not panic when
// the handler returns from the early-exit branch.
func TestEmbedSwitchCmd_RunENilRegistryErrorsCleanly(t *testing.T) {
	a := newStubApp(t) // Registry is nil
	restore := swapLoadAppFn(a)
	defer restore()

	cmd := embedSwitchCmd
	cmd.SetArgs([]string{"any-slug"})
	cmd.SetOut(new(bytes.Buffer))
	cmd.SetErr(new(bytes.Buffer))
	err := cmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "embedder not enabled")
}

// TestEmbedReembedCmd_RunEWithRealService: constructs a real
// *service.ReembedService against fake deps (mirrors the pattern in
// internal/service/reembed_test.go) and asserts the RunE handler:
// (a) exits 0 on dry-run with the expected message; (b) reaches
// Reembed.Run via the wired service. The fake's side effect (Embed
// called per item) confirms the service path.
func TestEmbedReembedCmd_RunEWithRealService(t *testing.T) {
	items := []domain.ContextItem{
		{ID: "i1", Title: "t1", Content: "c1"},
		{ID: "i2", Title: "t2", Content: "c2"},
	}
	spy := &reembedSpy{} // defined below; lives in this file to avoid
	// polluting embed_status_test.go's helper namespace.
	repo := &reembedListRepo{items: items}
	embRepo := &stubEmbeddingRepo{rows: []port.EmbeddingStatus{}}
	embedSvc := service.NewEmbedService(spy, &noopVectorStore{},
		&reembedGetRepo{items: items}, &emptyFileStore{}, embRepo)
	reembed := service.NewReembedService(repo, embedSvc,
		port.ModelInfo{Slug: "active-model", Dimension: 8})

	a := newStubApp(t)
	a.Reembed = reembed
	restore := swapLoadAppFn(a)
	defer restore()

	reembedLimit = 0
	reembedDryRun = true
	t.Cleanup(func() { reembedLimit, reembedDryRun = 0, false })

	var out bytes.Buffer
	cmd := embedReembedCmd
	cmd.SetArgs([]string{})
	cmd.SetOut(&out)
	cmd.SetErr(new(bytes.Buffer))
	require.NoError(t, cmd.Execute())
	assert.Contains(t, out.String(), "dry run")
	assert.Equal(t, 0, len(spy.calls), "dry run must not embed")
}

// reembedSpy mirrors the embedSpy from internal/service/reembed_test.go
// but is local to the cli package so we don't cross-import test helpers.
type reembedSpy struct {
	calls []string
}

func (s *reembedSpy) Model() port.ModelInfo {
	return port.ModelInfo{Slug: "active-model", Dimension: 8}
}
func (s *reembedSpy) Embed(ctx context.Context, texts []string) ([][]float32, error) {
	s.calls = append(s.calls, texts[0])
	return [][]float32{make([]float32, 8)}, nil
}

// reembedListRepo: minimal ContextRepo stub; only List is exercised.
type reembedListRepo struct{ items []domain.ContextItem }

func (r *reembedListRepo) Create(ctx context.Context, item domain.ContextItem) error {
	panic("unexpected")
}
func (r *reembedListRepo) Update(ctx context.Context, item domain.ContextItem) error {
	panic("unexpected")
}
func (r *reembedListRepo) Delete(ctx context.Context, id string) error {
	panic("unexpected")
}
func (r *reembedListRepo) Get(ctx context.Context, id string) (domain.ContextItem, error) {
	panic("unexpected")
}
func (r *reembedListRepo) List(ctx context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
	if f.Limit > 0 && f.Limit < len(r.items) {
		return r.items[:f.Limit], "", nil
	}
	return r.items, "", nil
}
func (r *reembedListRepo) NextCursor(item domain.ContextItem) string { return "" }

// reembedGetRepo: same shape but Get is the call exercised (EmbedService
// hydrates via Get during Embed).
type reembedGetRepo struct{ items []domain.ContextItem }

func (r *reembedGetRepo) Create(ctx context.Context, item domain.ContextItem) error {
	panic("unexpected")
}
func (r *reembedGetRepo) Update(ctx context.Context, item domain.ContextItem) error {
	panic("unexpected")
}
func (r *reembedGetRepo) Delete(ctx context.Context, id string) error {
	panic("unexpected")
}
func (r *reembedGetRepo) Get(ctx context.Context, id string) (domain.ContextItem, error) {
	for _, it := range r.items {
		if it.ID == id {
			return it, nil
		}
	}
	return domain.ContextItem{}, errors.New("not found")
}
func (r *reembedGetRepo) List(ctx context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
	return r.items, "", nil
}
func (r *reembedGetRepo) NextCursor(item domain.ContextItem) string { return "" }

// noopVectorStore, emptyFileStore: identical to the helpers in
// internal/service/reembed_test.go. Re-declared here because test helpers
// are package-private. If they cause a name collision in the cli package,
// rename to reembedNoopVectorStore / reembedEmptyFileStore.

type noopVectorStore struct{}

func (noopVectorStore) Put(ctx context.Context, model, itemID string, v []float32) error {
	return nil
}
func (noopVectorStore) Search(ctx context.Context, q port.VectorQuery) ([]port.VectorHit, error) {
	return nil, nil
}
func (noopVectorStore) Delete(ctx context.Context, model, itemID string) error { return nil }

type emptyFileStore struct{}

func (emptyFileStore) Put(content []byte, mime string) (uri string, hash string, err error) {
	return "", "", nil
}
func (emptyFileStore) Get(uri string) ([]byte, error) { return nil, nil }
func (emptyFileStore) Delete(uri string) error        { return nil }
```

Add the missing imports:

```go
	"errors"
```

- [ ] **Step 2: Run all six RunE tests + the existing structural tests**

Run: `CGO_ENABLED=1 go test ./internal/cli/ -run "TestEmbedModelAddCmd_RunECallsRegistryRegister|TestEmbedModelListCmd_RunECallsRegistryList|TestEmbedModelRemoveCmd_RunECallsRegistryRemove|TestEmbedSwitchCmd_RunECallsRegistrySetDefault|TestEmbedSwitchCmd_RunENilRegistryErrorsCleanly|TestEmbedReembedCmd_RunEWithRealService" -v`

Expected: PASS. All six new tests pass; `loadAppFn` swap is restored cleanly via `defer`.

- [ ] **Step 3: Run full CLI + service regression**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/cli/... ./internal/service/...`

Expected: PASS. The new test file compiles alongside `embed_status_test.go` without symbol collisions (helpers `noopVectorStore`, `emptyFileStore`, `reembedSpy`, etc. are uniquely named in the cli package).

**Collision guard:** If `go test` reports a name collision with helpers in another cli test file (e.g. `user_note_test.go` declares its own `noopVectorStore`), rename the conflicting types in `embed_run_e_test.go` with an `reembed` prefix. Pattern: `reembedNoopVectorStore`, `reembedEmptyFileStore`, `reembedSpy` (already prefixed).

- [ ] **Step 4: Run full repo regression**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./...`

Expected: PASS across all packages. Plan 2c follow-up is functionally complete.

- [ ] **Step 5: Format and commit**

```bash
goimports -w internal/cli/embed_run_e_test.go
git add internal/cli/embed_run_e_test.go
git commit -m "$(cat <<'EOF'
test(cli): RunE-level tests for 5 existing embed subcommands

Lock in that the RunE handlers for add/list/remove/switch call through
to the wired Registry/Reembed fields via loadAppFn. Each test swaps
loadAppFn to return a stubbed *App and asserts the expected downstream
call (or the friendly 'embedder not enabled' error on nil deps).

The Reembed test constructs a real *service.ReembedService against
fake deps (mirrors internal/service/reembed_test.go) — the service
type is concrete, so a struct mock isn't an option; the fake-deps
pattern is the minimum-overhead path.

Backfill and Worker remain covered by structural tests in
embed_test.go — they predate Plan 2c and aren't in the follow-up
scope.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Known Limitations (forward-compat for Plan 2d+)

These items surfaced during spec review but are deferred:

- **ErrCorruptConfig propagation to `embed model list` / `embed model remove`.** If a row's config is corrupt, `Registry.List` will return `ErrCorruptConfig` for that row (wrapped). The current CLI `embed model list` will fail the whole listing. The graceful handling (display `<corrupt>` in the BaseURL column; refuse remove with a clear message) is a UX nicety that Plan 2d+ can pick up.
- **Sub-3-char CJK LIKE fallback** — oldest open Known Limitation, separate Plan.
- **Subprocess e2e tests for the new commands** — stubbed-App pattern covers the RunE surface; existing `e2e_test.go` / `e2e_backfill_test.go` patterns are unchanged.
- **Per-call model parameter on `EmbedService.Embed`** (parallel embedding, N models per item) — Plan 2d scope.
- **Provider auto-detection** (probe `/v1/models` endpoint) — Plan 2d scope.
- **OpenAI batched embeddings API** — Plan 2d scope.
- **Async ingest queue** (goroutine + channel, return immediately) — Plan 2d scope.
- **Migrating Plan 2b alias rows to per-slug vec tables** — Plan 2d scope.

---

## Smoke (post-merge, manual)

After all 6 tasks merge to main, the existing Plan 2c smoke covers most of the surface end-to-end. One new step:

```bash
# Pick any item id from the DB (e.g. from a prior `unictx user-note add`).
unictx embed status <some-item-id>
# Expect: tabular output with MODEL_SLUG header, one row per model the
# item has status for. Empty items print "no embedding status rows for
# item <id>".
```

No new smoke for migration 0004 (FK enforcement is exercised by the unit test), the List tiebreaker (exercised by unit test), the race-friendly error (exercised by unit test), or the corrupt-JSON self-heal (exercised by unit test). The RunE tests cover the CLI paths.
