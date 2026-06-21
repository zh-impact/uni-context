# Changelog

Notable changes and known limitations per release. Dates are YYYY-MM-DD.

## Known Limitations

### Trigram FTS requires ≥3-character queries (affects 2-char CJK search)

The FTS5 index uses the `trigram` tokenizer for CJK-friendly matching.
Trigram indexes every contiguous 3-character sequence, so queries
shorter than 3 characters (e.g. the 2-character Chinese word `部署`)
silently return zero results — no error, just empty.

This affects:
- `unictx search <query>` where `len([]rune(query)) < 3`
- Any future caller of `SearchService.Search` with a short query

**Plan 2a update:** vector embeddings resolve this ONLY when the user
passes `--mode hybrid` (or programmatic callers select
`SearchModeHybrid`). The embedding model handles sub-word meaning
without a minimum-token rule, so hybrid search returns results for
2-char queries. Default `fts-only` mode is unchanged — short queries
still return zero. The LIKE-fallback option was considered and
rejected — LIKE on `title`/`content` would work for ASCII but scans
the whole table, and we'd be ripping it out anyway once vectors land.

Until you opt into `--mode hybrid` (see Plan 2a below), treat
sub-3-char queries as unsupported in the default search path. Search
results being empty for `部署` under `fts-only` is expected, not a bug.

## Plan 1 — Foundation (2026-06-19)

Initial release. CLI (`unictx`) for personal notes with FTS5 search,
SQLite persistence, hexagonal architecture. See
`docs/superpowers/plans/2026-06-19-foundation.md` for the plan and
`.superpowers/sdd/progress.md` for execution notes.

**Deferred to Plan 2** (from final review):
- Tags filter on search (note: Tags filter on `list` shipped in this
  patch series — see `ItemFilter.Tags`).
- 2-char CJK query support (see limitation above).
- Vector / hybrid search.

## Plan 2a — Hybrid Search (2026-06-20)

First vector-search release. Adds opt-in hybrid (FTS + vector KNN)
search on top of the Plan 1 foundation. See
`docs/superpowers/plans/2026-06-20-plan-2a-hybrid-search.md` for the
plan and `.superpowers/sdd/progress.md` for execution notes.

**What shipped:**
- **Single embedding model:** `bge-m3` via Ollama, 1024-dim, stored in
  sqlite-vec `vec_<model>` tables (migration 0002).
- **Synchronous embedding:** ingest blocks up to ~60s on Ollama per
  Create when the embedder is enabled. Failure is non-fatal — the item
  is still saved and FTS-searchable.
- **RRF hybrid search:** reciprocal-rank fusion (k=60) merges FTS5 and
  KNN top-k results with over-fetch at 3×limit.
- **`--mode hybrid` opt-in:** `unictx search --mode hybrid` (default
  remains `fts-only`). Programmatic callers pass `service.SearchModeHybrid`.
- **Error-tolerant degradation:** if the embedder is unreachable at
  search time, hybrid mode falls back to fts-only with a stderr warning
  rather than failing the query.
- **Config knob:** `embedder.enabled` (default `false`) gates all
  embedding behavior. Plan 1 users see no change until they flip it.

### Known Limitations (Plan 2a)

These are documented in code docstrings; reproduced here so a future
Plan 2b implementer can find them without grepping.

1. **Externalized-content items embed with EMPTY content.** When an
   item's content exceeds `domain.ContentInlineLimit` and is spilled to
   FileStore, only the title contributes to the vector
   (`internal/service/ingest.go` `contentForEmbed`). Large items become
   effectively title-only embeddings. **Plan 2b fix:** hydrate from
   FileStore (`port.FileStore.Get`) before embedding. **Status: closed
   in Plan 2b** — see EmbedService hydration below.

2. **`context_embedding` status rows are NOT written in 2a.** The
   schema has the table (migration 0002), but no code populates it —
   the presence of a `vec_<model>` row IS the embedded signal, and
   `context_item.any_embedding` is the coarse signal. **Plan 2b fix:**
   write status rows for retry tracking. **Status: closed in Plan 2b**
   — see port.EmbeddingRepo below.

3. **The hybrid e2e test is doubly gated** and is NOT exercised by any
   default `make` target. The RRF fusion path is unit-tested at the
   service layer only. To run the hybrid e2e:
   `CGO_ENABLED=1 go test -tags 'sqlite_fts5,integration,e2e' -run Hybrid ./internal/cli/...`
   with `UNICTX_E2E_HYBRID=1` set and a live Ollama with `bge-m3`
   pulled.

4. **Embedding is synchronous.** Ingest waits up to ~60s for Ollama on
   every Create when the embedder is enabled. **Plan 2b fix:** async
   queue. **Status: still synchronous in 2b** — async was descoped; 2b
   added retry tracking (worker) and bulk catch-up (backfill), but the
   ingest path still embeds inline. Ingest does not block on retry — a
   failed embed writes a `status='failed'` row that the worker picks up
   later. See Plan 2b Known Limitations.

5. **Only one embedding model.** The schema supports multi-model
   (`embedding_model` table with `vec_<model>` tables per row), but
   only `bge-m3` is wired. **Plan 2c fix:** runtime model registry.

6. **OpenAI-compat provider shipped as a Plan 2d preview.** The
   `openai` provider (`internal/adapter/embedder/openai`) supports any
   server exposing `POST /v1/embeddings`: LMStudio local, OpenAI hosted,
   vLLM, etc. Set `embedder.provider: openai` and `embedder.base_url`
   (default `http://localhost:1234/v1`, LMStudio's port). `api_key` is
   optional — local servers ignore it, OpenAI hosted requires it. What
   this preview does NOT include: provider auto-detection, OpenAI
   specific features (encoding formats, dimensions param, native
   batched calls beyond a single request), model catalog integration,
   error classification. Those remain Plan 2d.

7. **No backfill.** Plan 1 items created before enabling
   `embedder.enabled=true` will not be embedded. **Plan 2b fix:**
   `unictx embed backfill` command. **Status: closed in Plan 2b** —
   see `unictx embed backfill` below.

### Deferred to Plan 2b/c/d

Pulled from the plan's "Out of scope" section — still out of scope
after 2a:

- Async embedding queue → **Plan 2b**
- Backfill existing Plan 1 items (`unictx embed backfill`) → **Plan 2b**
- Embedding externalized (FileStore) content → **Plan 2b** (needs
  `FileStore.Get` in `EmbedService`)
- `context_embedding` status rows for retry tracking → **Plan 2b**
- Multi-model registry / runtime DDL → **Plan 2c**
- OpenAI-compat polish (provider auto-detection, encoding formats,
  dimensions param, model catalog integration, error classification).
  The core `openai` adapter shipped as a Plan 2d preview — see Plan 2a
  section above. → **Plan 2d**
- `--mode vector-only` → trivial follow-up, skipped in 2a

## Plan 2b — Async Embed Queue + Backfill (2026-06-21)

Closes four Plan 2a gaps: FileStore content hydration,
`context_embedding` status rows, `unictx embed backfill`, and
`unictx embed worker` (the retry loop that 2a's "async queue"
limitation gestured at). See
`docs/superpowers/plans/2026-06-21-plan-2b-async-backfill.md` for the
plan and `.superpowers/sdd/progress.md` for execution notes.

**What shipped:**
- **Migration 0003:** `context_embedding` gains `attempts` (INTEGER NOT
  NULL DEFAULT 0) and `last_error` (TEXT). Additive ALTER only — no
  rewrite of 0002. The original `error` column from 0002 is kept for
  backward-compat; `last_error` is what the worker updates on each
  failed retry.
- **`port.EmbeddingRepo`:** new single-responsibility port
  (`UpsertStatus` / `GetStatus` / `ListFailed`) separate from
  `ContextRepo`. The boundary matches the access pattern: ContextRepo
  owns item rows; EmbeddingRepo owns status rows. The sqlite adapter
  lives in `internal/adapter/sqlite/embedding_repo.go`.
- **`EmbedService` constructor change:** gained `port.FileStore` +
  `port.EmbeddingRepo` deps. `NewEmbedService(embedder, vs, repo, fs,
  embRepo)` — the two new trailing args close the Plan 2a "externalized
  items embed title-only" gap by hydrating content from FileStore
  before embedding (`hydrateContent` in
  `internal/service/embed.go`), and writes a status row on every
  attempt via `recordStatus` (`done` on success, `failed` with error
  text on failure).
- **`unictx embed backfill [--limit N] [--dry-run]`:** bulk-embeds
  items where `any_embedding=0`. Idempotent (the `AnyEmbedding=0`
  filter excludes items already embedded). Failures are recorded as
  status rows but do not abort the run — the summary at the end lists
  per-item failures.
- **`unictx embed worker [--interval 30s]`:** long-running retry loop
  for `status='failed'` rows. Polls `EmbeddingRepo.ListFailed` at the
  configured interval, retries each via `EmbedService.Embed` (which
  writes the new status row), and exits cleanly on SIGINT/SIGTERM via
  the shared `signalContext` helper.
- **`port.ItemFilter.AnyEmbedding`:** new `*int` field for backfill's
  "unembedded only" query. Default `nil` = no filter — Plan 1/2a
  callers pass `nil` and see no behavior change. Backfill sets it to
  `pointerTo(0)` to mean "any_embedding=0 only".

### Known Limitations (Plan 2b)

1. **Worker has no max-attempts cap.** A row stays `status='failed'`
   until it succeeds or the user manually `DELETE`s the row. Rationale:
   YAGNI; user controls worker lifetime via Ctrl+C. Plan 2e (if ever)
   could add `status='exhausted'` after N attempts.

2. **No exponential backoff.** Worker polls at a fixed interval
   (default 30s). Same YAGNI rationale as above — a stuck row just
   retries every 30s until the user kills the worker.

3. **Backfill + worker send one embed request per item.** No batched
   embeddings API call (OpenAI supports 1 request, N inputs). Plan 2d
   polish. Per-item error isolation is the trade-off.

4. **No `unictx embed status <id>` command.** Read-only inspection of
   `context_embedding` rows. Trivial follow-up; deferred to avoid scope
   creep. Use `sqlite3 unictx.db "SELECT * FROM context_embedding"` in
   the meantime.

5. **`EmbedService` constructor signature is a breaking change.** Plan
   2a had two callers (`app.Wire` + tests); both updated in this patch
   series. Any out-of-tree consumers (none known) would need the same
   update — add `port.FileStore` and `port.EmbeddingRepo` as the
   trailing two args.

6. **Ingest is still synchronous.** The 2a "async queue" limitation
   (#4 in 2a's list above) was only partially closed by 2b: 2b added
   retry tracking (worker) and catch-up (backfill), but the ingest path
   itself still blocks on the embed attempt before returning. The
   failure mode is graceful (status row written, item still
   FTS-searchable), but latency on embedder-enabled ingests is
   unchanged from 2a. A real async queue (goroutine + channel) is
   Plan 2e territory.

### Deferred to Plan 2c+

- Multi-model parallel embedding + per-model vec tables
- Re-embedding when switching models
- Provider auto-detection / encoding formats (OpenAI-compat polish)
- `unictx embed status <id>` (read-only status inspection)
- True async ingest queue (goroutine + channel, return immediately)

## Plan 2c — Multi-Model Registry & Migration (2026-06-21)

Replaces the Plan 2a `EnsureModelRegistered` placeholder with a runtime
model registry. Adds CLI commands for model lifecycle and migration.
On first Plan 2c run, `config.Embedder.Model` seeds the registry via
`reconcilePlan2cSync`; thereafter `embedding_model.is_default` is
authoritative, and only `embed switch` can change the active model. See
`docs/superpowers/specs/2026-06-21-plan-2c-multi-model-registry-design.md`
for the design.

**What shipped:**
- **`port.ModelRegistry` + sqlite impl:** add/get/list/default/update/
  remove with transactional DDL for per-slug `vec_<slug>_<dim>` tables.
  Shared-table protection preserves Plan 2b alias rows.
- **`unictx embed model add/list/remove`:** CLI surface for the registry.
- **`unictx embed switch <slug>`:** atomic is_default flip; prints a stderr
  reminder to follow with `embed reembed`.
- **`unictx embed reembed [--limit N] [--dry-run]`:** bulk re-embed items
  lacking a status='done' row for the active model. Reuses Plan 2b's
  status mechanism; resumable.
- **`service.ReembedService`:** Plan 2c sibling of BackfillService with a
  different filter (`NotDoneForModel` vs `AnyEmbedding=0`).
- **`app.Wire` reconciliation:** first Plan 2c run heals/registers the
  active model from `cfg.Embedder`, gated by
  `schema_meta.plan_2c_synced`. Subsequent runs trust the DB; the only
  way to change the active model is `embed switch`.

### Known Limitations (Plan 2c)

1. **API keys persist in `embedding_model.config` JSON.** The DB file
   contains them in plaintext. Set `unictx.db` permissions to 0600 on
   shared systems. OS keychain integration is a future plan.

2. **Only one active model at a time.** Parallel embedding (N models per
   item) is Plan 2d. Adding a model + switching requires `embed reembed`
   before vector search returns hits for existing items.

3. **`embed model remove` refuses shared vec_tables.** Plan 2b alias
   rows can share a vec_table with the seed; the registry detects this
   and errors out, requiring dependents to be removed first.

4. **Reconciliation runs once.** After `plan_2c_synced=1` is set, editing
   `embedder.model` in config has no effect. Use `embed switch`.

5. **Migration transition state.** Between `embed switch` and
   `embed reembed` completing, vector search returns 0 hits for the new
   model (SearchService hybrid mode falls back to fts-only gracefully).

### Deferred to Plan 2d+

- Parallel embedding (N models per item)
- Per-call model parameter on `EmbedService.Embed`
- Provider auto-detection (probe `/v1/models` endpoint)
- OpenAI batched embeddings API (1 request, N inputs)
- `unictx embed status <id>` (read-only status inspection)
- Migrating Plan 2b alias rows to per-slug vec tables

## Bugfix — OpenAI adapter string-error tolerance (2026-06-21)

Plan 2b verification surfaced a real adapter bug: LMStudio (and likely
other OpenAI-compat servers) sometimes return `200 OK` with the `error`
field as a bare string — observed during model loading and transient
internal errors. The adapter declared `embedResp.Error` as
`*struct{Message, Type}`, so the response body failed to decode with
`cannot unmarshal string into Go struct field embedResp.error`. The
caller saw a confusing decode error instead of the server's actual
message, and the row landed in `context_embedding` as `status='failed'`
with useless error text.

**What shipped** (commit `61d5b3c`, on `main` 2026-06-21):
- `embedResp.Error` is now `json.RawMessage` — the overall response
  decode succeeds regardless of the error field's shape (object or
  string).
- New `errorMessage()` helper extracts the human-readable message from
  either canonical object form (`{"message":"..."}`) or bare string
  (`"..."`).
- Both the non-200 path and the 200-with-empty-data path surface the
  extracted message. LMStudio's transient errors now reach the caller
  (and `context_embedding.last_error`) with their original text.

**Scope:** adapter-layer fix only. No schema change, no behavior change
on the happy path. Verified with 3 new unit tests covering string-error
on 200 OK, object-error on 200 OK, and string-error on non-200.

