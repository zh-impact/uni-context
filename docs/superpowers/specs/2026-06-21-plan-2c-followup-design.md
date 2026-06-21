# Plan 2c Follow-up — Design

**Date:** 2026-06-21
**Scope:** Cleanup + polish + one feature addition on top of Plan 2c (merged at `129b5cc`). Six items, one bundle, one PR.

## Motivation

Plan 2c shipped clean (whole-branch review APPROVED with fixes). The reviewer flagged three non-blocking items the implementer community would benefit from closing:

- **Important #1 → resolved in Fix #1** but the underlying schema mismatch (migration 0002 has no `ON DELETE CASCADE` on `context_embedding.model_slug`) is papered over with an explicit DELETE in `ModelRegistry.Remove`. Harden the contract with migration 0004 so the explicit DELETE becomes defensive rather than mandatory.
- **Important #2** — CLI tests for the new subcommands were purely structural (flags registered, command tree assembled). None invoked `RunE`. A `loadApp()` integration bug introduced by a future refactor would slip through.
- **Minor #4-6** — Concurrent `Register` race surfaces raw UNIQUE constraint text; `List` ordering has a tiebreaker-flake risk on slow CI; `scanModel` silently swallows corrupt JSON despite the spec promising graceful degradation + warning.

Plus one small feature that has been on the deferred list since Plan 2a: `unictx embed status <id>` for read-only status inspection.

## Scope

**In scope:**
1. Migration 0004 — `ON DELETE CASCADE` on `context_embedding.model_slug`.
2. Minor #4 — friendly error message on concurrent `Register` race.
3. Minor #5 — `List` tiebreaker `, slug ASC`.
4. Minor #6 — `scanModel` corrupt-JSON detection + reconcile self-heal + stderr warning.
5. `unictx embed status <id>` CLI command.
6. `loadAppFn` indirection in `embed.go` + RunE-level tests for all 6 embed subcommands.

**Out of scope:**
- Plan 2d (parallel embedding, provider auto-detection, batched embeddings API, async ingest queue).
- CLI e2e via subprocess (existing `e2e_test.go` / `e2e_backfill_test.go` pattern). Stubbed-App is sufficient per final-review #2.
- Migration of Plan 2b alias rows to per-slug vec tables.
- `port.ContractTest` for ContextRepo impls (Plan 1 deferred item).
- Sub-3-char CJK LIKE fallback (oldest open Known Limitation — separate Plan).

## Architecture

All changes are additive or in-place refinements to Plan 2c code. No new packages. No new dependencies.

- **One new migration** (0004) — standard SQLite table rebuild to add `ON DELETE CASCADE`.
- **One new CLI subcommand** (`embed status`) — read-only status inspection.
- **One new test infrastructure** (`loadAppFn` indirection in `embed.go` only) — enables RunE-level tests without subprocess overhead.
- **Three small refinements** to existing Plan 2c code.

No public API changes. Conventional commits per item.

## Components

### 1. Migration 0004 — `internal/adapter/sqlite/migrations/0004_embedding_model_slug_cascade.sql`

Standard SQLite rebuild dance (SQLite does not support `ALTER TABLE ... ADD FOREIGN KEY`):

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

The `migrations.go` runner gates each migration file by `schema_version`, so it runs exactly once per DB. No `IF NOT EXISTS` on the table name (would mask a half-applied previous run). The trailing `UPDATE schema_meta` follows the 0001/0003 pattern (the runner does not auto-bump the version).

### 2. `internal/port/embeddingrepo.go` — new method on `EmbeddingRepo`

```go
type EmbeddingRepo interface {
    UpsertStatus(ctx context.Context, itemID, modelSlug, status, errStr string) error
    GetStatus(ctx context.Context, itemID, modelSlug string) (EmbeddingStatus, error)
    ListFailed(ctx context.Context, limit int) ([]EmbeddingStatus, error)

    // ListForItem returns all status rows for the given item, ordered by
    // model_slug ASC. Empty slice (not nil) if no rows. Used by
    // `embed status <id>` to show per-model migration state.
    // Plan 2c follow-up addition.
    ListForItem(ctx context.Context, itemID string) ([]EmbeddingStatus, error)
}
```

### 3. `internal/adapter/sqlite/embedding_repo.go` — implementation

```go
func (r *EmbeddingRepo) ListForItem(ctx context.Context, itemID string) ([]port.EmbeddingStatus, error) {
    rows, err := r.db.QueryContext(ctx, `
        SELECT item_id, model_slug, embedded_at, status, attempts,
               COALESCE(last_error, '')
        FROM context_embedding
        WHERE item_id = ?
        ORDER BY model_slug ASC
    `, itemID)
    if err != nil {
        return nil, fmt.Errorf("list status for item %s: %w", itemID, err)
    }
    defer rows.Close()

    out := []port.EmbeddingStatus{}
    for rows.Next() {
        var s port.EmbeddingStatus
        if err := rows.Scan(&s.ItemID, &s.ModelSlug, &s.EmbeddedAt,
            &s.Status, &s.Attempts, &s.LastError); err != nil {
            return nil, fmt.Errorf("scan status row: %w", err)
        }
        out = append(out, s)
    }
    return out, rows.Err()
}
```

Note: returns empty slice (not nil) so the CLI can call `len(rows) == 0` without nil-checking.

### 4. `internal/cli/embed.go` — `loadAppFn` indirection

Add at file scope:
```go
// loadAppFn is the indirection that enables RunE-level tests. Tests swap
// it to return a stubbed *App; production code leaves the default. Only
// embed.go uses this — other CLI files call loadApp() directly.
var loadAppFn = loadApp
```

Replace all `loadApp()` calls within `embed.go`'s RunE handlers with `loadAppFn()`. This is a mechanical find-replace within the file; no semantic change.

### 5. `internal/cli/embed.go` — new `embedStatusCmd`

```go
var embedStatusCmd = &cobra.Command{
    Use:   "status <item-id>",
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
                r.ModelSlug, r.Status, r.Attempts, errCell, r.EmbeddedAt)
        }
        return w.Flush()
    },
}
```

Register in `init()`:
```go
embedCmd.AddCommand(embedStatusCmd)
```

`App.EmbeddingRepo` already exists (Plan 2b, nil when embedder disabled). Wire constructs `sqlite.NewEmbeddingRepo(db)` unconditionally on `cfg.Embedder.Enabled`. The CLI command references `a.EmbeddingRepo` directly — no App-struct change needed.

### 6. `internal/adapter/sqlite/model_registry.go` — three refinements

**a) `Register` race-friendly error.** In the `INSERT` error path:

```go
if _, err = tx.ExecContext(ctx, `INSERT INTO ...`, ...); err != nil {
    var sqliteErr *sqlite3.Error
    if errors.As(err, &sqliteErr) &&
        sqliteErr.ExtendedCode == sqlite3.ErrConstraintUnique {
        return fmt.Errorf("model %s already registered: %w", spec.Slug, err)
    }
    return fmt.Errorf("insert model row: %w", err)
}
```

Note: requires `github.com/mattn/go-sqlite3` import (vendored, already a transitive dep). The pre-check remains as a fast path for the common case.

**b) `List` tiebreaker.** Change SQL from:
```sql
ORDER BY created_at ASC
```
to:
```sql
ORDER BY created_at ASC, slug ASC
```

**c) `scanModel` corrupt-JSON sentinel.** Replace the swallow:
```go
_ = json.Unmarshal([]byte(cfg), &c)
```
with:
```go
if err := json.Unmarshal([]byte(cfg), &c); err != nil {
    return m, fmt.Errorf("%w: config JSON parse: %s", ErrCorruptConfig, err.Error())
}
```

Add a new sentinel:
```go
// ErrCorruptConfig signals that an embedding_model.config value could not
// be parsed as JSON. Callers may fall back to cfg.Embedder values or
// surface to the user. Used by reconcilePlan2cSync to self-heal.
var ErrCorruptConfig = errors.New("embedding_model.config corrupt")
```

Edge case: an empty config string is the seeded default (`'{}'`), which parses cleanly to a zero-value struct. Only non-empty non-JSON triggers the sentinel.

**d) `Remove` comment update.** Keep the explicit `DELETE FROM context_embedding WHERE model_slug = ?` and update the inline comment:
> Defense-in-depth. After migration 0004, the FK also cascades; this explicit DELETE ensures correctness even on DBs that pre-date 0004 or have FK enforcement off.

### 7. `internal/app/app.go` — reconcilePlan2cSync corrupt-JSON self-heal

The existing `reconcilePlan2cSync` (app.go:221-238) calls `reg.Get(ctx, cfg.Model)` and switches on the returned error. After Minor #6, `scanModel` returns `ErrCorruptConfig` for unparseable JSON, and `reg.Get` propagates it via `fmt.Errorf("get model %s: %w", slug, err)`. The `%w` verb preserves the sentinel so `errors.Is(getErr, ErrCorruptConfig)` matches. Add a fourth case between the existing `nil` and `ErrNotFound` paths:

```go
_, getErr := reg.Get(ctx, cfg.Model)
switch {
case getErr == nil:
    // Row exists and was scannable: heal provider + config from cfg.Embedder.
    if err := reg.UpdateConfig(ctx, cfg.Model, cfg.BaseURL, cfg.APIKey, cfg.Provider); err != nil {
        return fmt.Errorf("heal config for %s: %w", cfg.Model, err)
    }
case errors.Is(getErr, ErrCorruptConfig):
    // Row exists but config JSON is unreadable — UpdateConfig overwrites
    // the corrupt blob. Stderr warning surfaces the heal so the user
    // knows something touched their DB.
    fmt.Fprintf(os.Stderr,
        "warning: model %s has corrupt config JSON; healing from config.yaml\n", cfg.Model)
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

Note: `reg.GetActive` (app.go:113) also calls `scanModel`, so it too surfaces `ErrCorruptConfig` for a corrupt active row. The reconcile self-heal runs BEFORE `GetActive` is called in Wire, so the post-reconcile `GetActive` sees a healed row. Callers outside `Wire` (currently none — registry is only reached via Wire and the CLI, and the CLI uses List/Get which the spec's Risks section addresses) would surface the sentinel directly.

## Data flow

### Migration 0004 on existing user DB
```
User upgrades → Migrate runs → 0004 applies → context_embedding rebuilt with CASCADE
→ Future Remove(slug) calls → DELETE embedding_model row
→ FK CASCADE drops context_embedding rows automatically
→ Explicit DELETE in code is now redundant but kept as defense-in-depth
```

### `embed status <id>` happy path
```
User runs unictx embed status abc123
→ loadAppFn() returns *App (EmbeddingRepo non-nil)
→ a.EmbeddingRepo.ListForItem(ctx, "abc123")
→ SQL: SELECT ... WHERE item_id = ? ORDER BY model_slug ASC
→ Tabwriter output to stdout
```

### Corrupt-JSON reconciliation path
```
User has DB with manually-corrupted config JSON for active model
→ app.Wire → reconcilePlan2cSync
→ registry.Get(cfg.Model) → scanModel → JSON unmarshal fails → ErrCorruptConfig
→ reconcilePlan2cSync catches ErrCorruptConfig → stderr warning → UpdateConfig heals
→ SetDefault (idempotent)
→ schema_meta plan_2c_synced flag set
→ next Wire sees clean config, returns normally
```

### Race on Register
```
Two concurrent Register("X") calls
→ Both pre-checks see no row → both attempt INSERT
→ PK constraint: one wins, one fails with sqlite3.ErrConstraintUnique
→ Failure path detects UNIQUE via errors.As → rewrites to "model X already registered"
→ Loser's tx rolls back cleanly (vec table not created for the loser)
```

## Error handling matrix

| Scenario | Behavior |
|---|---|
| `embed status <missing-id>` | Exit 0; prints "no embedding status rows for item <id>" |
| `embed status` with disabled embedder | Exit 1; "embedder not enabled; set embedder.enabled=true in config" |
| `embed status abc 123` (2 args) | Cobra rejects: `accepts 1 arg(s), received 2` |
| Migration 0004 on corrupt DB | Fails cleanly with SQL error; `Migrate` returns error to caller |
| scanModel with empty `'{}'` config | Parses cleanly to zero-value; no ErrCorruptConfig |
| scanModel with non-empty non-JSON | Returns descriptor + ErrCorruptConfig; reconcile heals |
| Race Register (PK violation) | Loser sees "model X already registered: ..." (chained original) |
| List with tied created_at | Stable order via `, slug ASC` tiebreaker |
| Failed DB write mid-0004 migration | Runner's wrapping tx rolls back; new table not installed, old table intact. Next Wire re-runs Migrate, 0004 retries cleanly (file_version=4 > current=3). |

## Testing plan

### Unit tests

**Migration 0004 (`internal/adapter/sqlite/migrations/0004_embedding_cascade_test.go`):**
- Insert `context_item` + `embedding_model` + `context_embedding`.
- `DELETE FROM embedding_model WHERE slug = ?`.
- Assert `context_embedding` row count is now 0 (cascade fired).
- Also assert `context_item` delete still cascades its `context_embedding` rows.

**`internal/adapter/sqlite/embedding_repo_test.go`:**
- `TestEmbeddingRepo_ListForItem` covers: empty result (returns empty slice, not nil), single row, multiple models for same item, ordering by model_slug ASC.

**`internal/adapter/sqlite/model_registry_test.go`:**
- `TestModelRegistry_Register_ConcurrentSameSlugIsFriendly` — two goroutines `Register("X")`, loser's error message contains "already registered" (not raw UNIQUE constraint text).
- `TestModelRegistry_List_Tiebreaker` — two rows with identical `created_at` (set via direct INSERT), assert slug-ASC ordering.
- `TestModelRegistry_scanModel_CorruptJSONReturnsErrCorruptConfig` — direct unit test of scanModel: insert row with config = `'not json'`, call `Get`, assert `errors.Is(err, ErrCorruptConfig)`.
- `TestModelRegistry_scanModel_EmptyConfigIsOK` — config = `'{}'` parses cleanly, no sentinel.

**`internal/app/app_reconcile_test.go`:**
- `TestReconcilePlan2cSync_CorruptActiveConfig_HealsFromCfg` — pre-corrupt the active row's config to `'not json'`, run reconcile, assert config is healed to cfg.Embedder values + plan_2c_synced flag set.

### CLI RunE tests

**`internal/cli/embed_status_test.go`:**
- `TestEmbedStatusCmd_DisabledEmbedderErrorsCleanly` — stub App with `EmbeddingRepo == nil`, assert "embedder not enabled" error.
- `TestEmbedStatusCmd_NoRowsPrintsMessage` — stub returns empty slice, assert stdout contains "no embedding status rows".
- `TestEmbedStatusCmd_PrintsTabularOutput` — stub returns 2 rows, assert stdout contains MODEL_SLUG header + both rows.
- `TestEmbedStatusCmd_ArgCountRejected` — invoke with 2 args via `cmd.Execute()`, assert cobra's "accepts 1 arg(s)" error.

**`internal/cli/embed_run_e_test.go` (new file):**
One RunE test per existing subcommand. Each swaps `loadAppFn` to return a stub App:
- `TestEmbedModelAddCmd_RunECallsRegistryRegister` — assert stub Registry.Register called with spec from flags.
- `TestEmbedModelListCmd_RunECallsRegistryList` — assert stub Registry.List called, stdout contains table.
- `TestEmbedModelRemoveCmd_RunECallsRegistryRemove` — assert stub Registry.Remove called with arg.
- `TestEmbedSwitchCmd_RunECallsRegistrySetDefault` — assert stub Registry.SetDefault called, stderr contains reminder.
- `TestEmbedReembedCmd_RunECallsReembedRun` — assert stub Reembed.Run called with limit + dryRun.

All RunE tests use the `defer func() { loadAppFn = oldFn }()` pattern to restore production loader.

### Integration / e2e

None added. Stubbed-App pattern covers the RunE surface; existing subprocess e2e tests for backfill/worker are unchanged.

### Smoke

No new smoke step required. Existing Plan 2c smoke (`embed model list/add/switch/remove`) covers the registry end-to-end. The new `embed status` command is read-only; a quick manual `unictx embed status <some-id>` after a backfill confirms it works.

## Risks

1. **Migration 0004 partial failure.** The migrations runner wraps each file's body in a single tx (`migrations.go:97-107`); a power loss or mid-migration error rolls back, leaving the DB in the pre-0004 state (old `context_embedding` intact, `schema_version` still `3`). Next Wire re-runs Migrate, applies 0004 from scratch. Safe.

2. **`loadAppFn` race in tests.** Tests swap a package-level var; parallel tests in the same package would race. Mitigation: RunE tests are NOT marked `t.Parallel()`. Acceptable for this scope.

3. **ErrCorruptConfig propagation.** Other callers of `Get`/`GetActive`/`List` will now potentially see `ErrCorruptConfig`. Audit needed: CLI `embed model list` should display `<corrupt>` in the BaseURL column rather than fail; CLI `embed model remove` should refuse (don't drop a row whose config we can't read). These are minor UX concerns documented in the plan.

4. **mattn/go-sqlite3 import in model_registry.go.** The package is already imported by 7 files (`db.go`, several `_test.go`). Adding one more direct import is fine; `go mod tidy` promotes it from `// indirect` to direct in `go.mod`. No new module version.

## Out of scope (forward-compat for Plan 2d+)

- Parallel embedding (N models per item).
- Per-call model parameter on `EmbedService.Embed`.
- Provider auto-detection (probe `/v1/models` endpoint).
- OpenAI batched embeddings API.
- Async ingest queue (goroutine + channel, return immediately).
- Migrating Plan 2b alias rows to per-slug vec tables.
- Subprocess e2e tests for the new commands.
- Sub-3-char CJK LIKE fallback.
