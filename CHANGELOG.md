# Changelog

Notable changes and known limitations per release. Dates are YYYY-MM-DD.

## 2026-06-28 — Python migration complete (all 8 phases)

The Python implementation under `python/` is the project's primary,
reaching feature-parity with the archived Go implementation. All 8
phases of the migration plan are addressed.

- **Phase 8.1** (read-only parity verification) ran against a real
  Go-format DB at `~/dotfiles/local/share/unictx/unictx.db`
  (XDG_DATA_HOME override) — 19 user notes + 17 embeddings under the
  OpenAI default model. All read paths verified (doctor / list / get
  inline + externalized PDF / FTS + hybrid search with real RRF /
  embed model list / embed status with and without rows / reindex-fts
  --dry-run); surfaced and fixed one formatter bug (`embed status`
  crashed on int-typed `embedded_at`, commit `a4d2f32`).
- **Phase 8.2** (backup + read-write cutover) executed 2026-06-28:
  source DB + filestore backed up to `~/backups/unictx-2026-06-28/`
  (SHA-256 verified), then exercised the write path — `user note add`
  landed a new row + inline embedding + FTS index entry, `search`
  found it, `doctor` reported OK. Pre/post PRAGMA + FTS5
  integrity-check both clean. The Go binary was never built on this
  machine; Go source stays archived under `archive/go/` for reference.

**What shipped (577 tests passing across all phases):**

- **Phase 1** — Domain types, errors, Pydantic config (XDG-aware),
  distributed Protocols, shared test fixtures + fakes.
- **Phase 2** — Storage layer: SQLite connection factory (sqlite-vec
  loaded via `load_extension`), migration runner (0001-0004 with
  FTS5 missing-extension hint), ContextRepo (base36 cursor, scoped
  filters), Searcher (FTS5 + LIKE fallback for short queries), VectorStore
  (vec0 KNN), EmbeddingRepo (status rows + retry columns), ModelRegistry
  (atomic set_default), SchemaMeta.
- **Phase 3** — FileStore (content-addressed SHA-256), OllamaEmbedder
  (raw HTTP via httpx), OpenAIEmbedder (Bearer auth, base_url override).
- **Phase 4** — PDF engines: PyMuPDF (default; raises PdfEncrypted /
  PdfExtractionFailed with `reason` chained), shell (pdftotext spawn),
  http (service-based), factory with engine override.
- **Phase 5** — Services: IngestService (CRITICAL: PDF branch ordering
  + 3-blob rollback contract + ReindexFTS hook preserved), SearchService
  (RRF + 4 degradation paths), EmbedService (split vector/status writes),
  Worker/Backfill/Reembed (threading.Event cooperative cancellation),
  ReindexFTSService (bulk FTS rewrite), ItemService (query-side
  hydration), ModelService (model lifecycle), DiagnosticService
  (doctor backing).
- **Phase 6** — CLI (Typer): global flags + `wire()` factory +
  AppContainer, `user note add|get|list|delete`, `search` (FTS-only +
  hybrid), `embed model add|list|remove`, `embed switch|backfill|worker|
  reembed|status`, `doctor`, `reindex-fts`. All commands support `--json`.
- **Phase 7** — Test backfill: E2E flow tests (note lifecycle, large-
  content externalization round-trip, externalized-content FTS
  regression guard), edge-case gap closure.
- **Phase 8.1** — Read-only parity verification on real Go-format DB
  (19 notes, 17 embeddings). All read paths green; one formatter bug
  fixed in flight (`a4d2f32`: `embed status` plain-text mode called
  `.timestamp()` on an int — tests had masked it by passing datetime).

**Invariants preserved from Go** (verified via tests):

- PDF branch ordering: blob-PDF → blob-text → inline-text.
- 3-blob rollback contract (failures leave no orphan FileStore bytes).
- Embed-skip scope (image-only PDFs skip embed).
- RRF formula (`score = Σ 1/(k+rank)`).
- Base36 cursor format (Go-faithful encode/decode round-trip).
- Malformed-FTS bugfix (title-only snippet avoids SQLITE_CORRUPT_VTAB
  on externalized items).
- 50 MB file cap, mutual-exclusion rules, engine validation.

**P2 cleanup (2026-06-28):**

- **Plan 2c self-heal landed** (`bcd8419`). `wire()` now auto-registers
  the cfg-driven model when `embedder.enabled=True` and no row with
  that slug exists; sets it as default if no default exists. Idempotent
  and never overrides a user-chosen default. Folded in a related fix:
  `_build_embedder_from_active` now accepts `"openai-compat"` as an
  alias for `"openai"` (cfg vs DB historical spelling).
- **Concurrent register race — already covered.** The Python port's
  `register()` wraps INSERT in BEGIN/COMMIT and catches UNIQUE
  violations as `ModelConflict`, and `TestRaceProtection::
  test_integrity_error_on_insert_translates_to_conflict` already
  exercises the race path. The "no Go test exists" note was Go-only;
  no Python work was needed.
- **SearchHit DRY pass landed** (`4f80f8e`). Renamed the storage-side
  `title_snip` field to `snippet` to match the Protocol-side
  dataclass. The "title-only" rationale stays in the SQL comment and
  dataclass docstring. The CompositeSearcher adapter still exists to
  keep storage decoupled from the higher-layer Protocol, but no longer
  translates field names.

**Known limitations / deferred:**

- (none currently.)

## 2026-06-26 — Repo restructured to monorepo, Go archived

The Go implementation is now under `archive/go/` (frozen, no further
development). The repository is now a monorepo holding both the archived
Go implementation and the in-progress Python port side-by-side:

```
uni-context/
├── archive/go/             # frozen Go implementation (reference only)
├── python/                 # new primary implementation
│   └── spikes/migration-spike/   # pre-migration validation (6/6 passed)
├── docs/superpowers/
│   ├── plans/2026-06-26-python-migration.md   # revised plan
│   └── specs/2026-06-26-go-implementation-archive.md  # Go reference
└── README.md
```

The revised Python migration plan locks in a **modular monolith**
architecture (`items/ search/ embed/ pdf/ storage/ cli/`) instead of
the hexagonal layering Go used. Go's invariants (PDF branch ordering,
rollback contract, RRF formula, cursor format, malformed-FTS fix) are
preserved; structure is not. See plan §"Structure vs. Invariants" and
Go archive §1's "Python port uses a different STRUCTURE" note.

Git history of the Go code is preserved. The Python port has not yet
started execution.

## Known Limitations

### Trigram FTS requires ≥3-character queries (affects 2-char CJK search)

**Resolved for substring matching (2026-06-23):** queries shorter than 3
runes now fall back to a `LIKE %query%` scan against `title`, `summary`,
and `content`. 2-char CJK words like `部署` return results in both
`fts-only` and `hybrid` modes. The original trigram minimum still applies
to BM25 ranking and snippet extraction — LIKE hits get a flat score of
1.0 and empty snippet (callers fall back to `item.Title` for display).
LIKE wildcards in user input (`%`, `_`) are escaped via `ESCAPE '\'`.

**Original limitation (preserved for context):**

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
- Migrating Plan 2b alias rows to per-slug vec tables

## Plan 2c Follow-up — Cleanup, Polish, and `embed status` (2026-06-21)

Six-task TDD bundle closing Plan 2c's deferred items and tightening the
ragged edges surfaced during final review. See
`.superpowers/sdd/progress.md` for execution notes.

**What shipped:**
- **Migration 0004** (`internal/adapter/sqlite/migrations/0004_embedding_model_slug_cascade.sql`):
  `context_embedding.model_slug` FK is now `ON DELETE CASCADE`. The
  explicit `DELETE FROM context_embedding` inside `ModelRegistry.Remove`
  becomes defense-in-depth — still correct on DBs that pre-date 0004 or
  have FK enforcement off.
- **Race-friendly `Register`:** two concurrent `Register` calls that
  both lose the pre-check race now see
  `model <slug> already registered: <chained sqlite3 error>` instead of
  raw UNIQUE-constraint text. Detection via
  `errors.As(err, &sqliteErr) && sqliteErr.ExtendedCode == sqlite3.ErrConstraintUnique`.
  The pre-check is retained as the fast path.
- **`List` tiebreaker:** `ORDER BY created_at ASC, slug ASC` eliminates
  CI flake when rows share an epoch-second `created_at`.
- **Corrupt-config self-heal:** `scanModel` now surfaces
  `sqlite.ErrCorruptConfig` (exported sentinel) instead of silently
  returning empty BaseURL/APIKey. `reconcilePlan2cSync` catches the
  sentinel on first Wire, emits a stderr warning, and overwrites the
  config from `cfg.Embedder` via `UpdateConfig`.
- **New `unictx embed status <id>` command:** read-only tabular output
  of all `context_embedding` rows for an item, ordered by
  `model_slug ASC`. Backed by new `port.EmbeddingRepo.ListForItem`
  method (returns an empty slice, not nil).
- **RunE-level CLI tests:** `loadAppFn` indirection in `embed.go`
  enables RunE tests without subprocess overhead. The 5 existing
  subcommands (add/list/remove/switch/reembed) now have RunE coverage
  via a stubbed `*App`.

### Known Limitation (Plan 2c Follow-up)

- **`embed model list` and `embed model remove` surface raw error text
  when a row has corrupt config JSON.** Graceful handling (`<corrupt>`
  display / refuse-with-message) is deferred to Plan 2d+.

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


## Bugfix — LIKE fallback for short CJK queries (2026-06-23)

Resolves the long-standing Plan 1 limitation: 2-character CJK queries
like `部署` silently returned 0 results under `fts-only` mode because
the FTS5 trigram tokenizer requires queries of at least 3 runes.

**What shipped** (commit pending, on `main` 2026-06-23):
- `sqlite.Searcher.SearchFTS` now dispatches short queries
  (`utf8.RuneCountInString(query) < 3`) to a new `searchLike` path.
- LIKE pattern uses `%<escaped>%` against `title`, `summary`,
  `content`, with `%`, `_`, `\` escaped via `ESCAPE '\'`.
- Score is a flat 1.0 (no BM25 ranking); snippet is empty (service
  layer's title fallback covers display).
- 3-rune queries with leading/trailing whitespace (e.g. `部署 `) stay
  on the FTS path — the existing trigram-phrase behavior is preserved.
- Hybrid mode benefits transparently: RRF folds LIKE hits into the
  fusion with `1/(rank+60)` weight, same as FTS hits.

Verified with 4 new searcher tests: short CJK match, short ASCII match,
wildcard escaping, and a regression guard that 3+ rune queries still
produce non-empty snippets via the FTS path.


## Bugfix — Externalized content unsearchable via FTS (2026-06-23)

When an item's content exceeded `domain.ContentInlineLimit` (4KB) it was
externalized to FileStore with `item.Content=""`. The AFTER INSERT trigger
on `context_item` reads `new.content` when writing the FTS row, so the
FTS index captured `""` and `search "<keyword>"` returned 0 hits even
when the keyword existed in the externalized file. Embeddings were
unaffected — Plan 2b's `EmbedService` hydrates from FileStore — making the
bug FTS-specific and easy to miss.

**What shipped** (commit `3159020`, on `main` 2026-06-23):
- New `port.ContextRepo.ReindexFTS(ctx, id, title, summary, content)`
  rewrites the FTS row via FTS5's delete-then-insert special-command
  pattern (external-content tables cannot be UPDATEd). Idempotent.
- `IngestService.Create` calls `ReindexFTS` after a successful
  `repo.Create` when `item.ContentURI != ""`, rewriting the FTS row with
  the in-memory `in.Content` before it goes out of scope. Failure is
  non-fatal — the item is already saved; `unictx reindex-fts` can heal
  it later.
- New `service.ReindexFTSService` walks all items, hydrates externalized
  content from FileStore, and calls `ReindexFTS`. Constructed
  unconditionally in `app.Wire` (FTS is available in Plan 1 too).
- New `unictx reindex-fts [--limit N] [--dry-run]` CLI command for
  one-shot backfill of legacy data. Inline items are skipped (trigger
  already handled them); failures are recorded per-item without aborting
  the run.

**Backfill applied to user data:** 2 externalized notes (a resume
markdown and a long text note) were reindexed on 2026-06-23 via
`unictx reindex-fts`. Both are now searchable by their content keywords.

**Scope:** search-only fix. No schema change, no behavior change on the
embed path. Verified with 2 new sqlite tests (externalized→searchable +
idempotency), 1 new service integration test (verified the test fails
without the fix), and 4 new service tests for the bulk runner.

## PDF Attach for `user note add` (2026-06-26)

`unictx user note add --file paper.pdf` now extracts text and stores both
the original PDF blob and the extracted text as a searchable, embeddable
context item. The PDF is content-addressed in FileStore under
`SourceMeta["original_uri"]`; the extracted text is stored as the item's
`Content` (externalized via the existing 4KB threshold) so FTS, hybrid
search, and embeddings all work without special-casing.

**Engines** (`pdf.engine` in config):
- `gxpdf` — pure-Go default (`github.com/coregx/gxpdf`), no external
  dependencies. Handles text-layer PDFs; returns empty string for
  image-only / scanned PDFs.
- `shell` — subprocess (e.g. `pdftotext - -`); configured via
  `pdf.engines.shell.command`. 30s default timeout.
- `http` — POST binary to a service; configured via
  `pdf.engines.http.url` (optional `auth_token`). 30s default timeout.

Per-call override: `unictx user note add --file x.pdf --engine shell`.

**Behavior:**
- Encrypted PDFs surface a clear "encrypted pdf" error (no `--password`
  flag yet — see spec "Future work").
- Image-only / scanned PDFs (empty extraction) store the blob with
  empty `Content` and a warning logged to stderr; embed is skipped to
  avoid title-only vectors polluting the index.
- File size cap bumped 10 MB → 50 MB for realistic PDF sizes (academic
  papers 5-15 MB, scanned textbooks 20-80 MB).
- `mimeForTextFile` renamed to `mimeForFile` (the old name lied once it
  returned `application/pdf`). Unknown extensions still fall back to
  `text/plain` for backward compat.

**Rollback contract:** when `repo.Create` fails after the PDF branch ran,
`IngestService.Create` deletes both the externalized text (`item.ContentURI`)
and the PDF blob (`SourceMeta["original_uri"]`) from FileStore. Without
this, the failure path would orphan refcount=1 entries.

**Empty `pdf.engine` (the default) disables PDF support** —
`user note add --file x.pdf` errors with a clear "pdf extraction not
configured" until the user opts in.

**Scope:** 8 tasks under `feat/pdf-attach`. Verified with 9 new adapter
tests, 7 new ingest service tests, 3 new app wiring tests, and 3 new
CLI integration tests; full suite green across 11 packages with
`-tags sqlite_fts5`. See `docs/superpowers/plans/2026-06-26-pdf-attach.md`
for the plan and `.superpowers/sdd/progress.md` for execution notes.

**Out of scope (deferred to future work):**
- `--password` for encrypted PDFs (gxpdf API surface needs evaluation)
- `--pages 1-10` page-range selection
- Other binary formats (docx, html)
- `--no-size-limit` escape hatch

## Bugfix — Externalized content returns "database disk image is malformed" from search (2026-06-26)

When `unictx search <query>` matched an item whose content was externalized
to FileStore (>4KB → empty `context_item.content`, real text in FileStore
under `content_uri`), the SQL query aborted with
`Error: fts: database disk image is malformed`. The bug was latent before
PDF Attach (3 of 11 rows in the dev DB were already externalized) but
surfaced widely once PDF ingestion made externalization the common case.

**Root cause:** `context_fts` is configured as an FTS5 external-content
table (`content='context_item', content_rowid='rowid'`). The 2026-06-23
"Externalized content unsearchable via FTS" bugfix made `MATCH` find the
row by calling `ReindexFTS` to rewrite the FTS row with the extracted
text directly (bypassing the `AFTER UPDATE` trigger). That left
`context_fts_data` holding tokens that `context_item.content` (now empty)
no longer contained. FTS5's `snippet(context_fts, 2, ...)` on the content
column detects the divergence at query time and returns
`SQLITE_CORRUPT_VTAB`, which SQLite surfaces as the misleading "disk
image is malformed" error.

**What shipped:**
- `internal/adapter/sqlite/searcher.go:searchSQL` drops
  `snippet(context_fts, 2, ...)` (the content-column snippet). Title
  snippet stays — title is always inline in `context_item` and the
  integrity check doesn't fire there.
- Untitled notes still match via FTS (the inverted index spans all
  columns); they just have an empty `Snippet` field. The CLI display
  already falls back to `item.Title`.
- New regression test `TestSearcher_FTS_ExternalizedContentDoesNotCorrupt`
  asserts: externalized content findable via MATCH; title snippet
  populated; no error. Note: the test environment (mattn/go-sqlite3)
  handles the divergence differently than system SQLite, so the test
  cannot directly reproduce the malformed error — but it pins the new
  contract (no content-column snippet call) and the findability guarantee.

**Follow-up tracked (Option B from the fix discussion):** restore content
snippets by generating them in Go from hydrated content. Requires wiring
`ItemService` or FileStore into `SearchService.hydrate`. Not blocking;
current behavior matches what `unictx user note get` returns for the same
item once the display layer falls back to `item.Title`.
