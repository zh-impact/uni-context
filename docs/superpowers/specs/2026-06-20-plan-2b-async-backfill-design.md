# Plan 2b — Async Embedding Queue + Backfill — Design

> **For agentic workers:** This is a design spec, not an implementation plan. The plan lives at `docs/superpowers/plans/2026-06-20-plan-2b-async-backfill.md` (to be written by `superpowers:writing-plans`).

## Motivation

Plan 2a ships sync embedding with four known gaps (documented in `CHANGELOG.md` Plan 2a section):

1. **Externalized content embeds empty.** Items whose content exceeds `domain.ContentInlineLimit` spill to `port.FileStore`. Plan 2a's `EmbedService` only sees `item.Content` (empty after externalization), so the vector is built from title alone. Large notes get poor vector recall.

2. **No `context_embedding` status rows.** Migration 0002 created the table but no code populates it. There is no durable record of which items failed to embed or why. Retry is impossible.

3. **No backfill.** Items created before `embedder.enabled=true` stay at `any_embedding=0` forever. Users enabling embedding after the fact get hybrid search that misses every pre-existing item.

4. **No async path.** Plan 2a blocks ingest for up to ~60s per Create when the embedder is slow or unreachable. If the embedder is down at ingest time, the item is saved but never retried.

Plan 2b closes all four gaps with a single architectural addition: status rows as the source of truth for embed state, plus two CLI commands that consume them (`backfill`, `worker`).

## Architecture

```
ingest (sync, unchanged)
  → EmbedService.Embed(itemID)
      ├─ hydrate content from FileStore if item.Content == "" && ContentURI != ""
      ├─ call embedder.Embed
      ├─ vs.Put                       (vec table write)
      ├─ repo.Update any_embedding=1
      └─ embeddingRepo.UpsertStatus   (status='done' or 'failed' + error)

unictx embed backfill [--limit N] [--dry-run]
  → BackfillService.Run
      for each item where any_embedding=0 (LIMIT N if set):
        call EmbedService.Embed inline
        log progress every 100 items

unictx embed worker [--interval 30s]
  → WorkerService.Run (long-running, Ctrl+C to stop)
      loop:
        SELECT failed embeddings ordered by embedded_at ASC
        for each:
          retry EmbedService.Embed
          on success: UpsertStatus(status='done', attempts=N+1)
          on failure: UpsertStatus(status='failed', attempts=N+1, embedded_at=now)
        sleep(interval)
```

### Status row policy

Write a `context_embedding` row for **every embed attempt** — success and failure. The table becomes the complete log of embed attempts; the `vec_<model>` row's presence is no longer load-bearing for "is this item embedded?".

`context_item.any_embedding` remains the per-item coarse flag (`SearchService` consults it for fast filtering). The two layers serve different consumers:

- `any_embedding` — fast SQL filter, no JOIN needed
- `context_embedding` — durable per-(item, model) state for retry + observability

### Sync vs async boundary

Plan 2b keeps ingest **synchronous**. Rationale:

- Single-user CLI; one embed round-trip is ~100ms with local Ollama, ~500ms with remote LMStudio — acceptable latency for `unictx user note add`.
- A goroutine in a short-lived CLI process dies on exit, leaving no durable "in-flight" state. The complexity of background embedding isn't worth it for this scale.
- The worker command exists for the case where sync embed *failed* (embedder unreachable at ingest time) and the user wants to retry later without re-running ingest.

This trades Plan 2a's "embed fails → warn → done" for "embed fails → status row → recoverable later". Same UX for the common path; better recovery for the failure path.

### Backfill: inline, not two-step

`unictx embed backfill` embeds items inline (sync per item). It does NOT write 'pending' rows for a separate worker to process. Rationale:

- Backfill is itself a long-running command; why require two?
- The worker exists for a *different* use case (retry failures during normal ingest). Conflating them complicates both.
- A two-step design (backfill → worker) doubles the user's mental model.

If backfill hits a transient embedder failure mid-run, it writes a status='failed' row and continues; user can run `worker` later to mop up.

## Schema changes (migration 0003)

The existing `context_embedding` table from migration 0002:

```sql
CREATE TABLE context_embedding (
    item_id     TEXT NOT NULL REFERENCES context_item(id) ON DELETE CASCADE,
    model_slug  TEXT NOT NULL REFERENCES embedding_model(slug),
    embedded_at INTEGER NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    PRIMARY KEY (item_id, model_slug)
);
```

Add retry-tracking columns via additive `ALTER TABLE`:

```sql
-- migration 0003_embedding_retry.sql
ALTER TABLE context_embedding ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE context_embedding ADD COLUMN last_error TEXT;
```

- `attempts` — count of embed attempts (success or failure). Caps not enforced in 2b; worker retries indefinitely until user Ctrl+C. (A future Plan could add `status='exhausted'` after N attempts, but YAGNI for 2b.)
- `last_error` — most recent error text. The original `error` column stays for backward-compat with any 0002-era rows (there shouldn't be any, since 2a never wrote rows, but defensive).

### UPSERT semantics

`UpsertStatus(itemID, model, status, error)`:

```sql
INSERT INTO context_embedding
    (item_id, model_slug, embedded_at, status, error, last_error, attempts)
VALUES (?, ?, ?, ?, ?, ?, 1)
ON CONFLICT(item_id, model_slug) DO UPDATE SET
    embedded_at = excluded.embedded_at,
    status      = excluded.status,
    error       = excluded.error,
    last_error  = excluded.last_error,
    attempts    = context_embedding.attempts + 1;
```

The `attempts + 1` increment happens in the conflict branch — a fresh INSERT starts at 1.

## Components

### `port.EmbeddingRepo` (new)

Single-responsibility port for `context_embedding` table access. Lives separately from `ContextRepo` because:

- Different lifecycle: `ContextRepo` owns `context_item`; `EmbeddingRepo` owns the embedding status join table. Mixing them produces a fat interface.
- Easier to test: fake `EmbeddingRepo` without faking all of `ContextRepo`.
- Follows the existing pattern (ContextRepo, ProjectRepo, Searcher, VectorStore are separate ports).

```go
type EmbeddingStatus struct {
    ItemID     string
    ModelSlug  string
    Status     string  // "done" | "failed"
    Error      string
    Attempts   int
    EmbeddedAt time.Time
}

type EmbeddingRepo interface {
    UpsertStatus(ctx context.Context, itemID, modelSlug, status, errStr string) error
    GetStatus(ctx context.Context, itemID, modelSlug string) (EmbeddingStatus, error)
    ListFailed(ctx context.Context, limit int) ([]EmbeddingStatus, error)
}
```

Methods are the minimum needed by EmbedService (UpsertStatus) and WorkerService (ListFailed). `GetStatus` is included because backfill uses it to short-circuit re-embedding items that already succeeded (defensive — `any_embedding=1` is the primary signal, but status row check is cheap and authoritative). A future `unictx embed status <id>` command will need a `ListByItem` method; deferred to that plan to avoid YAGNI.

### `service.EmbedService` (modified)

Constructor changes to accept `port.FileStore` and `port.EmbeddingRepo`:

```go
func NewEmbedService(
    embedder port.Embedder,
    vs port.VectorStore,
    repo port.ContextRepo,
    fs port.FileStore,        // NEW: for hydration
    embRepo port.EmbeddingRepo, // NEW: for status rows
) *EmbedService
```

`Embed(ctx, itemID, title, content)` changes:

1. **Hydration**: if `content == ""`, fetch via `repo.Get(itemID)` → if `ContentURI != ""`, call `fs.Get(ContentURI)` → use that. Caller can still pass content directly (backfill path may already have it in memory).
2. **Status write**: after `vs.Put` succeeds → `UpsertStatus(itemID, model, "done", "")`. After any failure → `UpsertStatus(itemID, model, "failed", err.Error())` then return the original error.

The status write itself can fail (e.g., DB issue); that failure is logged to stderr but does NOT mask the original embed result.

### `service.BackfillService` (new)

```go
type BackfillService struct {
    repo   port.ContextRepo
    embed  *EmbedService
}

func (s *BackfillService) Run(ctx context.Context, limit int, dryRun bool) (BackfillReport, error)

type BackfillReport struct {
    Scanned   int
    Embedded  int
    Skipped   int  // already had any_embedding=1
    Failed    int
    Failures  []BackfillFailure
}
```

Iterates items where `any_embedding=0`, ordered by `created_at ASC` (oldest first — they've been waiting longest). For each:

- If `dryRun`: count, don't embed
- Else: call `EmbedService.Embed(ctx, item.ID, item.Title, "")` (let EmbedService hydrate content)
- Log progress to stderr every 100 items
- On embed failure: record in `Failures`, continue

`limit <= 0` means no limit.

### `service.WorkerService` (new)

```go
type WorkerService struct {
    embRepo port.EmbeddingRepo
    embed   *EmbedService
}

func (s *WorkerService) Run(ctx context.Context, interval time.Duration) error
```

Long-running loop. Each iteration:

1. `ListFailed(ctx, batchSize=100)` — ordered by `embedded_at ASC` (oldest failures first)
2. For each: retry `EmbedService.Embed`. UpsertStatus reflects new state (done or failed+attempts++).
3. `select { case <-ctx.Done(): return; case <-time.After(interval): }`

Ctrl+C cancellation via `signal.NotifyContext` in the CLI handler.

### CLI

New parent command `unictx embed` with two subcommands:

```
unictx embed backfill [--limit N] [--dry-run]
unictx embed worker [--interval 30s]
```

(`embed status <id>` deferred — listed as Plan 2c-or-later.)

`embed` parent has no RunE; only subcommands are callable. Cobra handles this naturally with `&cobra.Command{...}` having subcommands but no Run.

### Wiring (`app.Wire`)

Construct `EmbeddingRepo`, pass to `EmbedService` along with `FileStore`. Construct `BackfillService` and `WorkerService`, expose on `App` struct so CLI handlers can reach them.

When `embedder.enabled=false`: skip constructing all of the above. `App.Backfill` and `App.Worker` stay nil; CLI commands error cleanly with "embedder not enabled".

## Data flow

### Normal ingest (embedder enabled)

```
unictx user note add "long text..." (length > ContentInlineLimit)
  → IngestService.Create
      → domain.NewContextItem
      → fs.Put(content)            → item.ContentURI set, item.Content = ""
      → repo.Create(item)
      → EmbedService.Embed(item.ID, title, "")
          → repo.Get(item.ID)
          → content == "" && ContentURI != "" → fs.Get(uri) → hydrated
          → embedder.Embed([title + "\n\n" + hydrated])
          → vs.Put(vec)             → vec_bge_m3_1024 row
          → repo.Update any_embedding=1
          → embRepo.UpsertStatus("done")  ← NEW
```

If embedder fails:
```
          → embedder.Embed errors
          → embRepo.UpsertStatus("failed", err)  ← NEW
          → return error (caller logs warning, item still saved)
```

### Backfill

```
unictx embed backfill --limit 1000
  → BackfillService.Run(limit=1000, dryRun=false)
      → SELECT id FROM context_item WHERE any_embedding=0 ORDER BY created_at ASC LIMIT 1000
      → for each id:
          EmbedService.Embed(id, title, "")  // hydration inside
          log every 100
      → return BackfillReport
  → CLI prints report summary
```

### Worker

```
unictx embed worker --interval 30s
  → WorkerService.Run(ctx, 30s)
      loop:
        → ListFailed(100)
        → for each failed:
            EmbedService.Embed
            UpsertStatus (done or failed+attempts++)
        → if no failures found: log "queue empty"
        → sleep 30s (cancellable)
```

## Error handling

| Failure point | Behavior |
|---|---|
| Embedder unreachable during ingest | Status row 'failed'; item saved; `worker` retries later |
| Embedder returns wrong-dim vector | Status row 'failed'; vec0 rejects Put; `worker` will keep failing until config fixed — user can `DELETE FROM context_embedding WHERE item_id=...` to skip |
| FileStore.Get fails during hydration | Status row 'failed'; logged; item stays unembedded |
| DB write fails for status row itself | Log to stderr, do not mask embed result; vec row + any_embedding are still authoritative in that case |
| Worker context cancelled (Ctrl+C) | Drain current batch, exit cleanly |
| Backfill hits unrecoverable embedder error | Continue to next item; record failure; report at end |

## Testing

- **Unit (`embed_test.go`)**: hydration path, status row on success, status row on failure, attempts counter increments on retry.
- **Unit (`backfill_test.go`)**: skips any_embedding=1, processes only unembedded, --dry-run doesn't embed, --limit honored, progress callback fires.
- **Unit (`worker_test.go`)**: drains failed rows in priority order, exits on ctx.Done, retries same row across iterations until success.
- **Integration**: full recovery flow — disable embedder, ingest 3 items (status='failed'), re-enable, run worker, verify all 3 flip to 'done'.
- **E2E (gated `UNICTX_E2E_BACKFILL=1`)**: real LMStudio backfill 50 items, assert all reach 'done'.

## Out of scope (deferred)

- Background goroutine in normal CLI commands — rejected explicitly; sync + worker covers the use case.
- Exponential backoff — worker uses fixed interval; user controls lifetime via Ctrl+C.
- Max-attempts cap with `status='exhausted'` — YAGNI for 2b.
- `unictx embed status <id>` — read-only, trivial follow-up; deferred to avoid scope creep.
- Re-embedding when switching models — Plan 2c (multi-model registry).
- Multi-model parallel embedding — Plan 2c.
- OpenAI batched embeddings API (1 request, N inputs) — Plan 2d polish; 2b sends one request per item to keep error isolation simple.

## Risks

1. **`UpsertStatus` failure masks embed success.** Mitigation: log status write failure to stderr; vec row + any_embedding remain valid. Worker's `ListFailed` will eventually pick it up if the row was never written.

2. **`EmbedService` constructor signature change** breaks Plan 2a call sites. Plan 2a has exactly two callers: `app.Wire` and tests. Both updated in the same patch.

3. **Worker running while user ingests concurrently** could double-embed the same item. Mitigation: vec0 Put is DELETE+INSERT in tx (already idempotent); UpsertStatus is `ON CONFLICT DO UPDATE`; both safe under concurrent calls.

4. **Backfill on large corpus** (10k+ items) could take hours. Mitigation: `--limit` flag caps the run; progress logging; user can Ctrl+C and resume (status rows persist).
