# Plan 2c — Multi-Model Registry & Migration — Design

> **For agentic workers:** This is a design spec, not an implementation plan. The plan lives at `docs/superpowers/plans/2026-06-21-plan-2c-multi-model-registry.md` (to be written by `superpowers:writing-plans`).

## Motivation

Plan 2a/2b ship a single hard-coded embedding model (`bge-m3`, 1024-dim). Three pain points show up as soon as a user wants to try a different model:

1. **`EnsureModelRegistered` is a placeholder.** It reuses `vec_bge_m3_1024` for any 1024-dim slug — silently wrong for any cross-model comparison (different vector spaces). For non-1024 dims (768, 1536, 3072) it errors out with no path forward.
2. **No way to register a second model.** Config is single-model; no CLI surface to add/switch/remove.
3. **No migration path.** Switching models means all existing embeddings become useless (wrong vector space) and the user has no command to re-embed in bulk under a new model.

Plan 2c closes all three by introducing a runtime model registry with proper per-slug vec tables and CLI commands for migration. The design center is **single-active-model with switch + bulk re-embed** — not parallel embedding (deferred to Plan 2d if ever needed).

## Scope

**In scope:**
- `port.ModelRegistry` interface + sqlite implementation.
- Per-slug vec tables created at runtime via DDL (no migration file needed).
- CLI: `unictx embed model add/list/remove`, `unictx embed switch`, `unictx embed reembed`.
- One-time config reconciliation on first Plan 2c run (via `schema_meta` flag).
- `ReembedService` for bulk re-embed under the active model.

**Out of scope (deferred):**
- Parallel embedding (one item embedded by N models simultaneously) → Plan 2d.
- Per-call model parameter on `EmbedService.Embed` → Plan 2d.
- Provider auto-detection / encoding formats / batched API → Plan 2d.
- Auto-cleanup of orphaned vec tables → manual `embed model remove`.
- `unictx embed status <id>` (read-only status inspection) → trivial follow-up.

## Architecture

```
                    app.Wire (每次 CLI 调用)
                              │
        ┌─────────────────────┴──────────────────────┐
        │  1. SELECT slug, dim, provider, base_url,  │
        │     api_key FROM embedding_model           │
        │     WHERE is_default = 1                   │
        │  2. 构造对应的唯一 port.Embedder            │
        │  3. EmbedService / SearchService 用它      │
        └────────────────────────────────────────────┘

  embed model add <slug> --provider --base-url --dim [--api-key]
    → INSERT embedding_model row (is_default=0)
    → CREATE VIRTUAL TABLE IF NOT EXISTS vec_<slug>_<dim> USING vec0(...)

  embed model list
    → SELECT slug, provider, dimension, vec_table, is_default, status

  embed switch <slug>
    → BEGIN; UPDATE ... SET is_default=1 WHERE slug=?;
      UPDATE ... SET is_default=0 WHERE slug<>?; COMMIT
    → 不重嵌入。下次 CLI 调用读到新 default。

  embed reembed [--limit N] [--dry-run]
    → ReembedService iter: WHERE NOT EXISTS status='done'
      for active model，逐个 embed
    → 失败写 status='failed' 行；可恢复（worker 后续兜底）

  embed model remove <slug>
    → 拒绝 is_default=1 的（要求先 switch 走）
    → 拒绝 vec_table 被其他 slug 引用的（防止误删共享表）
    → DROP TABLE vec_<slug>_<dim>; DELETE embedding_model row
    → context_embedding 行靠 FK ON DELETE CASCADE 自动清
```

### Key principles

- **One active embedder at a time.** Picked at startup from `embedding_model WHERE is_default=1`.
- **`port.Embedder` interface unchanged.** Multiple distinct `Embedder` instances per registered model; only the active one is constructed at Wire time. This keeps `EmbedService` / `SearchService` signatures untouched.
- **`EmbedService.Embed(ctx, itemID, title, content)` signature unchanged.** It embeds into `s.embedder.Model().Slug`. The active model is determined by which embedder was wired, not by a per-call parameter.
- **Metadata changes are atomic and instant.** `embed switch` is just a transactional UPDATE; no re-embed work.
- **Bulk work is separate and resumable.** `embed reembed` reuses the Plan 2b status-row mechanism.
- **No schema migration.** Existing `embedding_model` + `context_embedding` + vec0 tables are multi-model-capable.

## Components

### `internal/port/modelregistry.go` (new)

```go
// ModelDescriptor is the full projection of an embedding_model row.
type ModelDescriptor struct {
    Slug      string
    Name      string
    Provider  string
    BaseURL   string // from config JSON column
    APIKey    string // from config JSON column
    Dimension int
    VecTable  string
    IsDefault bool
    Status    string // "active" | "disabled"
}

// ModelSpec is the input to Register.
type ModelSpec struct {
    Slug      string
    Provider  string
    BaseURL   string
    APIKey    string
    Dimension int
}

type ModelRegistry interface {
    List(ctx context.Context) ([]ModelDescriptor, error)
    GetActive(ctx context.Context) (ModelDescriptor, error) // is_default=1
    Get(ctx context.Context, slug string) (ModelDescriptor, error)
    Register(ctx context.Context, spec ModelSpec) error  // strict INSERT; errors if slug exists
    UpdateConfig(ctx context.Context, slug, baseURL, apiKey, provider string) error // heal existing row
    SetDefault(ctx context.Context, slug string) error   // atomic flip
    Remove(ctx context.Context, slug string) error       // refuses default + shared vec_table
}
```

### `internal/adapter/sqlite/model_registry.go` (rewrite)

Replaces the `EnsureModelRegistered` placeholder. Implementation notes:

- `Register`:
  - **Strict INSERT semantics**: if slug already exists → return error "model already registered". Caller is responsible for any upsert/UPDATE orchestration (see `reconcilePlan2cSync` below for the one place that does upsert).
  - Compute `vecTable := "vec_" + dashToUnderscore(slug) + "_" + dim`.
  - Single transaction: `INSERT embedding_model row` + `CREATE VIRTUAL TABLE IF NOT EXISTS <vecTable> USING vec0(item_id TEXT PRIMARY KEY, embedding FLOAT[<dim>] distance_metric=cosine)`.
  - Config JSON column stores `{"base_url": "...", "api_key": "..."}`.
  - Provide a separate `UpdateConfig(ctx, slug, baseURL, apiKey, provider string) error` method for the heal path. Kept out of `Register` so the strict-insert contract stays clean.
- `SetDefault`: `BEGIN; UPDATE ... SET is_default=1 WHERE slug=?; UPDATE ... SET is_default=0 WHERE slug<>?; COMMIT`.
- `Remove`:
  - Pre-checks (outside tx): slug exists; slug not is_default=1; `SELECT COUNT(*) FROM embedding_model WHERE vec_table = ?` ≤ 1 (reject if shared).
  - Tx: `DROP TABLE <vecTable>` + `DELETE FROM embedding_model WHERE slug=?`. FK ON DELETE CASCADE cleans `context_embedding` rows.
- `GetActive` / `Get` / `List`: SELECT + scan into ModelDescriptor, parsing config JSON for BaseURL + APIKey.
- `dashToUnderscore("text-embedding-3-large")` → `"text_embedding_3_large"` → vec table `vec_text_embedding_3_large_3072`.

### `internal/app/app.go` (Wire changes)

```go
func Wire(cfg *config.Config) (*App, error) {
    // ... existing DB/fs/repo wiring ...

    var embedder port.Embedder
    var embedSvc *service.EmbedService
    // ... existing embedder-related decls ...

    if cfg.Embedder.Enabled {
        registry := sqlite.NewModelRegistry(db)

        // First-Plan-2c-run reconciliation. gated by schema_meta flag
        // so we never override an explicit `embed switch` from a prior run.
        if err := reconcilePlan2cSync(ctx, db, registry, cfg.Embedder); err != nil {
            return nil, fmt.Errorf("plan 2c sync: %w", err)
        }

        active, err := registry.GetActive(ctx)
        if err != nil {
            return nil, fmt.Errorf("read active model: %w", err)
        }

        // Construct embedder for the active model's provider + base_url + api_key.
        switch active.Provider {
        case "ollama":
            embedder = ollama.New(active.BaseURL, active.Slug, active.Dimension)
        case "openai":
            embedder = openai.New(active.BaseURL, active.Slug, active.Dimension, active.APIKey)
        default:
            return nil, fmt.Errorf("unsupported provider %q for active model %q",
                active.Provider, active.Slug)
        }

        // ... rest unchanged: EnsureModelRegistered call REMOVED (registry replaces it) ...
        // ... embeddingRepo, embedSvc, backfill, worker constructed as before ...
    }

    // ... rest unchanged ...
}

// reconcilePlan2cSync runs once on first Plan 2c invocation.
// If schema_meta.plan_2c_synced != '1':
//   1. SELECT embedding_model WHERE slug = cfg.Embedder.Model
//      a. If not exists → Register(cfg.Embedder fields) — creates per-slug vec table.
//      b. If exists → UpdateConfig(slug, cfg.Embedder.BaseURL, APIKey, Provider)
//         — overwrites the row's provider + config JSON. This heals Plan 2b
//         alias rows whose config was '{}'.
//   2. SetDefault(cfg.Embedder.Model) — atomic flip; idempotent if already default.
//   3. INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('plan_2c_synced', '1').
// After first run, DB is authoritative; config (except `enabled`) is ignored.
func reconcilePlan2cSync(ctx context.Context, db *sql.DB, reg port.ModelRegistry, cfg config.EmbedderConfig) error { ... }
```

### `internal/service/reembed.go` (new)

```go
type ReembedService struct {
    repo   port.ContextRepo
    embed  *EmbedService
    active port.ModelInfo // slug + dim of the currently-wired embedder
}

type ReembedReport struct {
    Scanned  int
    Embedded int
    Failed   int
    Failures []ReembedFailure
}

// Run iterates items where NOT EXISTS a status='done' row for the active
// model_slug, ordered by created_at ASC. For each: EmbedService.Embed
// (which embeds under s.embedder.Model().Slug == active slug).
// Idempotent: re-runs skip items already done for the active model.
// Resumable: failures write status='failed' rows; worker picks them up later.
func (s *ReembedService) Run(ctx context.Context, limit int, dryRun bool) (ReembedReport, error)
```

`ReembedService` is intentionally separate from `BackfillService` because the filter differs:
- `BackfillService` (Plan 2b): `WHERE any_embedding = 0` — first-time embed never-embedded items.
- `ReembedService` (Plan 2c): `WHERE NOT EXISTS (status='done' AND model_slug=active)` — migrate items to the active model regardless of any_embedding state.

### `internal/cli/embed.go` (extend)

```
embed                                 (parent, no Run)
├── backfill                          (Plan 2b, unchanged)
├── worker                            (Plan 2b, unchanged)
├── reembed   [--limit N] [--dry-run]   ← new
├── switch    <slug>                    ← new
└── model                             (parent, no Run) ← new
    ├── add    <slug> --provider --base-url --dim [--api-key]
    ├── list
    └── remove <slug>
```

`embed switch` prints to stderr: `Active model switched to <slug>. Run 'unictx embed reembed' to migrate existing items.`

### Unchanged

- `internal/port/embedder.go` — `Embedder` interface stays single-model.
- `internal/service/embed.go` — `EmbedService` constructor + Embed method unchanged.
- `internal/service/search.go` — `SearchService` unchanged.
- `internal/service/backfill.go` — Plan 2b backfill unchanged.
- `internal/service/worker.go` — Worker is already model-agnostic (iterates all `status='failed'` rows regardless of model_slug).
- `internal/adapter/sqlite/embedding_repo.go` — `EmbeddingRepo` unchanged.
- `internal/adapter/sqlite/migrations/` — no new migration file.

## Data flow

### Flow A: Fresh install (post-Plan-2c)

```
migrations run → 0002 seeds bge-m3 row (is_default=1, vec_bge_m3_1024)
Wire:
  1. registry.GetActive() → bge-m3 row
  2. schema_meta 'plan_2c_synced' unset → reconcilePlan2cSync:
     · cfg.Embedder.Model exists in DB? → UpdateConfig (heals provider + config JSON)
     · cfg.Embedder.Model missing?       → Register (creates row + per-slug vec table)
     · SetDefault(cfg.Embedder.Model) — idempotent if bge-m3 already default
     · Set schema_meta 'plan_2c_synced' = '1'.
  3. Read final active row → construct port.Embedder.
  4. Wire EmbedService / SearchService.
```

### Flow B: Existing Plan 2b user upgrades (the project author's case)

```
DB state: bge-m3 (is_default=1, ollama, vec_bge_m3_1024)
          + text-embedding-bge-m3 (alias, is_default=0, vec_bge_m3_1024, config='{}')
config:   provider=openai, base_url=LMStudio, model=text-embedding-bge-m3, dim=1024

Wire:
  1. registry.GetActive() → bge-m3 row
  2. plan_2c_synced unset → reconcilePlan2cSync:
     · cfg.Embedder.Model = "text-embedding-bge-m3" exists in DB (Plan 2b alias row)
     · UpdateConfig("text-embedding-bge-m3", LMStudio, "", "openai"):
       UPDATE row SET provider='openai', config=JSON({"base_url":"http://LMStudio:1234/v1","api_key":""})
     · SetDefault("text-embedding-bge-m3") → bge-m3 is_default=0, text-embedding-bge-m3 is_default=1
     · vec_table untouched (text-embedding-bge-m3 row still points at vec_bge_m3_1024)
  3. Read active = text-embedding-bge-m3 → construct openai embedder for LMStudio URL
  4. Wire services

Result: user perceives no behavior change (same LMStudio, same vec_bge_m3_1024).
```

### Flow C: Add a new model

```
$ unictx embed model add text-embedding-3-large \
    --provider openai --base-url https://api.openai.com/v1 \
    --dim 3072 --api-key sk-...

  registry.Register:
    · INSERT embedding_model row (is_default=0, config=JSON({base_url, api_key}))
    · CREATE VIRTUAL TABLE IF NOT EXISTS vec_text_embedding_3_large_3072 USING vec0(
        item_id TEXT PRIMARY KEY,
        embedding FLOAT[3072] distance_metric=cosine
      )

$ unictx embed model list
  → table: slug / provider / dim / vec_table / is_default / status
```

### Flow D: Switch active model

```
$ unictx embed switch text-embedding-3-large

  registry.SetDefault:
    BEGIN;
      UPDATE embedding_model SET is_default=1 WHERE slug='text-embedding-3-large';
      UPDATE embedding_model SET is_default=0 WHERE slug<>'text-embedding-3-large';
    COMMIT;
  Returns immediately. No re-embed.
  Stderr: "Active model switched. Run 'unictx embed reembed' to migrate existing items."

Next CLI invocation:
  Wire reads active = text-embedding-3-large → constructs corresponding embedder
  ⚠ vec_text_embedding_3_large_3072 is empty → vector search returns 0 hits
    until reembed completes (SearchService falls back to fts-only gracefully).
```

### Flow E: Bulk re-embed

```
$ unictx embed reembed --limit 1000

  ReembedService.Run(ctx, limit=1000, dryRun=false):
    iter: WHERE NOT EXISTS (
            SELECT 1 FROM context_embedding
            WHERE item_id = ci.id AND model_slug = ? AND status = 'done'
          )
          ORDER BY created_at ASC LIMIT ?      -- ? = active.slug, limit
    for each item:
      EmbedService.Embed(ctx, id, title, "")
        → embedder is active (text-embedding-3-large)
        → vec written to vec_text_embedding_3_large_3072
        → status row: model_slug='text-embedding-3-large', status='done'
      log progress to stderr every 100 items
      on failure: status='failed'; continue

  Returns ReembedReport {Scanned, Embedded, Failed, Failures}
```

Idempotent: re-runs skip items already done for the active model.
Resumable: failed rows are picked up by `unictx embed worker` (which is model-agnostic).

### Flow F: Remove a model

```
$ unictx embed model remove bge-m3

  registry.Remove:
    1. SELECT is_default, vec_table FROM embedding_model WHERE slug='bge-m3'
    2. If is_default=1 → error "cannot remove default model; switch first".
    3. SELECT COUNT(*) FROM embedding_model WHERE vec_table = ? → if >1:
         error "vec table <name> shared by N models; remove dependents first".
    4. BEGIN;
         DROP TABLE <vec_table>;
         DELETE FROM embedding_model WHERE slug='bge-m3';
       COMMIT;
       -- context_embedding rows for bge-m3 cascade-deleted via FK.
```

## Error handling

| Failure point | Behavior |
|---|---|
| `embed model add` slug already exists | `INSERT OR IGNORE` → affected rows = 0 → error "model already registered" |
| `embed model add` CREATE VIRTUAL TABLE fails | Transactional rollback; no partial state in DB |
| `embed switch <unknown-slug>` | Pre-check `SELECT slug FROM embedding_model WHERE slug=?` → NotFound error |
| `embed switch` followed by search without reembed | Vector search returns 0 hits (new table empty); SearchService hybrid mode already falls back to fts-only. CHANGELOG notes this transition state. |
| `embed reembed` mid-run embedder failure | Failed item gets status='failed'; command continues; report lists failures. Worker picks them up later. |
| `embed model remove` on is_default=1 | Reject; require switch first |
| `embed model remove` on shared vec_table | Reject; require removing dependents first. Protects Plan 2b alias rows. |
| Wire with empty DB (theoretical; 0002 seed prevents) | `reconcilePlan2cSync` registers from `cfg.Embedder` + SetDefault + writes plan_2c_synced=1 |
| Wire with active row config JSON corrupt (missing `base_url` key or empty value) | Fall back to cfg.Embedder values + log warning |
| API key persistence in DB | Stored in `embedding_model.config` JSON. CHANGELOG notes: ensure DB file permissions are tight (0600). |

## Plan 2b alias sharing protection

The Plan 2b `EnsureModelRegistered` placeholder created alias rows that point at `vec_bge_m3_1024` for any 1024-dim slug. The project author's DB has exactly this: `text-embedding-bge-m3` row pointing at `vec_bge_m3_1024`.

Plan 2c must not break this:
- Wire keeps using the existing alias row's vec_table.
- `embed model remove` checks for vec_table sharing before dropping.
- A future Plan 2d cleanup could migrate alias rows to per-slug tables if needed; Plan 2c doesn't touch them.

## Testing

| Layer | Tests |
|---|---|
| **Unit: `sqlite.ModelRegistry`** | Register new slug → row exists + vec table created; Register existing slug → error; SetDefault → atomic flip + only one is_default=1; Remove on default → rejected; Remove on non-default → DROP TABLE + DELETE row + cascade-clean status rows; Remove on shared vec_table → rejected |
| **Unit: `service.ReembedService`** | Iter condition excludes already-done for active model; `--dry-run` doesn't write; `--limit` honored; failure-continue + status='failed' written; progress callback fires |
| **Unit: `app.Wire` reconciliation** | Fresh DB + cfg.Embedder.Model='bge-m3' → no new row, plan_2c_synced=1; fresh DB + cfg.Embedder.Model='custom' → Register + SetDefault; existing alias row → config JSON healed |
| **Unit: `cli`** | `embed model add` arg parsing + registry calls; `embed model list` output format; `embed switch` error paths; `embed model remove` rejection paths |
| **Integration** | Full migration: add B → switch → reembed (fake embedder) → verify B's status rows + vec rows; then remove A |
| **E2E (gated `UNICTX_E2E_MIGRATION=1`)** | Real LMStudio: add model A → ingest N items → switch → reembed → verify search hits |

The integration test should use a fake embedder (pre-canned vectors) to avoid coupling to a live server.

## Risks

1. **First-Plan-2c-run sync surprises the user.** If `cfg.Embedder.Model` differs from the seeded `bge-m3`, Wire will flip is_default on first run. Mitigation: log to stderr what's happening; document in CHANGELOG. The project author's case is exactly this scenario and the behavior is the expected one.

2. **API key leaks via DB file.** Stored in `embedding_model.config` JSON column. Mitigation: CHANGELOG note recommending 0600 perms on `unictx.db`; future Plan could store keys in OS keychain instead.

3. **User removes a model whose vec_table is shared.** Mitigation: `Remove` rejects with a clear message; user must remove dependents first.

4. **User switches without reembed and forgets.** Vector search returns 0 hits silently. Mitigation: `embed switch` prints stderr reminder; future Plan could add a `--require-reembed` flag that refuses switch without a follow-up reembed ack.

5. **`reconcilePlan2cSync` runs more than once.** Mitigation: schema_meta flag is checked first; once set, the function is a no-op.

6. **`vec0` module not registered.** Already handled by Plan 2a (`sqlite_vec.Auto()` in `db.go` init). Verified by `vec_smoke_test.go`.

## Out of scope

- Parallel embedding (N models per item) → Plan 2d.
- Per-call model parameter on `EmbedService.Embed` → Plan 2d.
- Provider auto-detection (e.g. probe `/v1/models` endpoint) → Plan 2d.
- OpenAI batched embeddings API → Plan 2d.
- Auto-cleanup of orphaned vec tables → manual `embed model remove`.
- `unictx embed status <id>` → trivial follow-up plan.
- Migrating Plan 2b alias rows to per-slug vec tables → Plan 2d cleanup.
