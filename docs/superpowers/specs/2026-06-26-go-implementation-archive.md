# Go Implementation Archive — Design Reference for Python Port

> **Purpose:** Capture every load-bearing design decision, invariant, and
> hard-won lesson from the Go implementation so the Python port doesn't
> rediscover them. Read this BEFORE writing any port code.
>
> **Status as of 2026-06-26:** Go implementation is the source of truth.
> The Python port must preserve every invariant listed here unless it
> consciously decides not to (and notes the deviation).

## 1. Architecture Overview

Hexagonal / DDD-clean. Six layers, dependencies always point inward:

```
cli  ─────────►  app (DI wiring)  ─►  service  ─►  port (interfaces)
                     │                  │
                     ▼                  ▼
                  adapter            domain (pure)
                  (sqlite,
                   fsstore, …)
```

- **`domain/`** — pure types: `ContextItem`, `Project`, `Scope/Kind/Source`
  enums, `NewContextItem` constructor with `validateCombination`. No I/O.
- **`port/`** — `Protocol`-equivalent interfaces: `ContextRepo`, `ProjectRepo`,
  `Searcher`, `VectorStore`, `Embedder`, `FileStore`, `EmbeddingRepo`,
  `ModelRegistry`, `PDFExtractor`, `SchemaMeta`. Plus DTOs (`ItemFilter`,
  `SearchQuery`, `ModelInfo`, `ModelSpec`).
- **`adapter/`** — concrete implementations:
  - `sqlite/` — repo, searcher, vectorstore, embedding_repo, model_registry,
    migrations (embed.FS)
  - `fsstore/` — sha256 content-addressed file store with refcounting
  - `embedder/ollama`, `embedder/openai`, `embedder/fake`
  - `pdf/gxpdf`, `pdf/shell`, `pdf/http`
- **`service/`** — use cases: `Ingest`, `Search`, `Embed`, `Worker`,
  `Backfill`, `Reembed`, `ReindexFTS`, `Item`, `Diagnostic`, `Model`.
  Constructors take `log io.Writer` as last param before variadic opts.
- **`app/`** — DI wiring (`Wire(cfg) (*App, error)`). Owns concrete types
  and lifecycle (constructs adapters, injects into services).
- **`cli/`** — cobra commands. Touches only services, never raw ports
  (post-2026-06 refactor). Subcommands under `user note`, `search`,
  `embed`, `doctor`, `reindex-fts`.

**Test surface:** 49 source files / 47 test files. Test packages mirror
source packages (`service_test`, `cli_test`-style internal tests). Most
packages use `:memory:` SQLite with `mattn/go-sqlite3` + `-tags sqlite_fts5`.

## 2. Schema (Migrations)

Four migrations, applied in order. **All four are reusable verbatim in
the Python port** — schema is identical regardless of language. Migration
runner logic must be ported (embed.FS, version comparison, transactional
exec, FTS5-missing hint).

### Migration 0001 — Foundation

- `context_item` table with `id TEXT PRIMARY KEY` (UUIDv7), `title`,
  `summary`, `content`, `content_mime`, `tags JSON`, `source_meta JSON`,
  `created_at INTEGER` (Unix ts), `updated_at INTEGER`, `version INTEGER`,
  FKs to `project(id)`. Owner column is `owner_user_id`.
- `project` table.
- `context_fts` virtual table:
  ```sql
  CREATE VIRTUAL TABLE context_fts USING fts5(
      title, summary, content,
      content='context_item', content_rowid='rowid',
      tokenize='trigram'
  );
  ```
  Three triggers: `context_ai` (AFTER INSERT), `context_ad` (AFTER DELETE),
  `context_au` (AFTER UPDATE — delete-then-insert pair). The `_au` trigger
  is what makes `UPDATE context_item SET content=''` reindex the FTS row
  to empty, **breaking FTS findability** — this is why the 2026-06-23
  bugfix used a separate `ReindexFTS` path instead.
- `schema_meta(key, value)` table; `schema_version='1'` after this migration.

### Migration 0002 — Embeddings

- `embedding_model(slug TEXT PK, name, provider, dim INT, is_default INT,
  vec_table TEXT, config JSON, created_at INT)`. Seeds `bge-m3` as default.
- `context_embedding(item_id, model_slug, embedded_at, status, attempts
  INT DEFAULT 0, last_error)` — wait, `attempts` and `last_error` were
  added in 0003. In 0002 only `item_id, model_slug, embedded_at, status`.
- `vec_bge_m3_1024` virtual table via vec0 module. Same author's Python
  package creates identical schema — verified in spike.
- ⚠️ **Known issue:** 0002 doesn't bump `schema_version`. Means Migrate
  re-runs 0002 on every Migrate call until 0003 applies. Harmless
  (`IF NOT EXISTS` / `INSERT OR IGNORE`), but tracked as forward tech
  debt. The Python port can choose to fix this by adding a `schema_version`
  bump inside 0002 — but **only if 0002 hasn't run anywhere yet**. Don't
  edit shipped migrations on a DB that's already been migrated.

### Migration 0003 — Embedding retry columns

- Adds `attempts INT DEFAULT 0` and `last_error TEXT` to
  `context_embedding`. Bumps version to `3`.

### Migration 0004 — model_slug FK CASCADE

- Adds `ON DELETE CASCADE` to `context_embedding.model_slug` FK.
  Previously only `item_id` cascaded; `ModelRegistry.Remove` had to
  manually delete context_embedding rows.
- Bumps version to `4` (current).

## 3. Load-Bearing Invariants

These are correctness properties that MUST be preserved. Violating them
silently breaks data.

### 3.1 Externalization Threshold

`domain.ContentInlineLimit = 4 * 1024` (4 KB). `IngestService.Create`
checks `len(in.Content)` against this BEFORE calling `NewContextItem`.
Content larger than this → externalized to FileStore (sha256-addressed,
refcounted), `item.Content = ""`, `item.ContentURI = "file://<sha256>"`.

**Why this matters for the port:** if you change the threshold, existing
items stay where they are but new items follow the new rule. Mixed-DB
behaviour is fine. Don't reduce below 4 KB — FTS5 indexed content fits
inline there, and you'd externalize short notes that worked fine.

### 3.2 FTS5 + External-Content + ReindexFTS — The Tricky Combination

`context_fts` is an **external-content FTS5 table** (`content='context_item'`).
This means:

- The inverted index (`context_fts_data`) stores tokens.
- `snippet(context_fts, N, ...)` reads from `context_item.title/summary/content`
  at query time, NOT from the inverted index.

For externalized items (`context_item.content = ""` but FileStore has the
bytes), the AFTER INSERT trigger writes an empty FTS row. The 2026-06-23
bugfix introduced `repo.ReindexFTS(id, title, summary, content)` to
rewrite the FTS row with the hydrated bytes from FileStore. **This bypasses
the AFTER UPDATE trigger** — it directly INSERTs into `context_fts` to
swap the row.

Result: inverted index has tokens, external content table is empty.
FTS5 detects the divergence when `snippet(context_fts, 2, ...)` (the
content column) is called and returns `SQLITE_CORRUPT_VTAB`, surfaced as
`database disk image is malformed`. We hit this in production on
2026-06-26 after PDF Attach made externalization common. **Spike confirms
the bug reproduces 100% in Python** — same SQLite library underneath.

**Python port rule:** `searchSQL` MUST NOT call `snippet(context_fts, 2, ...)`.
Only `snippet(context_fts, 0, ...)` (title column) is safe — title is
always inline. See Go commit `706de09` for the reference fix.

### 3.3 IngestService.Create PDF Branch Ordering

```go
func (s *IngestService) Create(ctx, in Input, opts ...CreateOption) (string, error) {
    // 1. PDF branch (if any) — extracts text, stores blob, sets in.Content,
    //    in.SourceMeta["original_uri"], in.ContentMIME = "text/plain".
    //    MUST run BEFORE NewContextItem because:
    //    - countWords reads in.Content for item.WordCount
    //    - externalize reads in.Content to decide inline vs FileStore
    if isPDF(in.MIME) && s.pdfExtractor != nil { ... }

    // 2. NewContextItem builds the item (WordCount computed here).
    item, err := domain.NewContextItem(...)

    // 3. Externalize if in.Content > ContentInlineLimit.
    // 4. repo.Create(item) → triggers AFTER INSERT (writes empty/inline FTS row).
    // 5. If externalized, repo.ReindexFTS(item.ID, ..., in.Content) — fixup.
    // 6. If embedSvc != nil, embed (unless embed-skip applies).
}
```

Two option types because Go has no overloading:
- `IngestOption` (`WithPDFExtractor`) — constructor-level, applied in `Wire`
- `CreateOption` (`WithExtractor`) — per-call, used by CLI `--engine` override

### 3.4 Rollback Contract

If `repo.Create` fails after the PDF branch ran, `fs.Delete` BOTH
`item.ContentURI` (the externalized extracted text) AND `pdfURI` (the
PDF blob URI from `SourceMeta["original_uri"]`). Otherwise you orphan
refcount=1 FileStore entries.

### 3.5 Embed-Skip Scope

When the PDF branch produced empty extracted text (image-only / scanned
PDF), the embed pipeline MUST skip — otherwise the embedder receives
`Title + "" + ""` (no content, no content_uri) and produces a title-only
vector that pollutes the index.

The Go predicate is `pdfURI != "" && item.Content == "" && item.ContentURI == ""`.
The scope is critical: applying this skip on the non-PDF path would
break embeddings for any empty-content note.

### 3.6 Cursor Format — Base-36, not Decimal

```go
// internal/adapter/sqlite/repo.go:encodeCursor
return strconv.FormatInt(ts, 36) + ":" + id
```

Python equivalent (verified byte-identical in spike):

```python
def encode_cursor(ts: int, item_id: str) -> str:
    # ... hand-rolled base36 to match strconv.FormatInt semantics
```

`str(ts)` produces decimal and **breaks pagination** on existing data.
Use the verified encoder from `spikes/python-migration/spike.py`.

### 3.7 SearchService Per-Leg Timeout

Hybrid search runs FTS + Vector in parallel-ish. Each leg gets a
timeout (default 5s, configurable). If the vector leg fails or times
out, fall back to FTS-only with a warning. If the FTS leg fails,
continue with vector-only. Never abort the whole search because one leg
failed.

**Python port:** `asyncio.wait_for` works, but if you go sync-first
(recommended) use `concurrent.futures.ThreadPoolExecutor` with timeout
for the HTTP embedder call.

### 3.8 RRF Fusion Formula

`score = Σ 1 / (rank + K)` where K=60 (industry standard). Items in
both FTS and vector lists get two contributions. **Rank must be
post-filter** (after scope/kind filters applied), not pre-filter —
otherwise filtered-out items skew the rank counter. We fixed this in
commit `d85dc2b` after the bug was found.

### 3.9 Search LIMIT Semantics

`clampLimit(n)`:
- `n <= 0` → return `defaultLimit` (20)
- `n > 200` → clamp to 200 (was: reset to 20 — buggy, silently destroyed
  over-fetch headroom; fixed in commit `4d26cea`)
- otherwise → `n` unchanged

The Service layer over-fetches 3× the user's limit (to give post-filter
trimming headroom). The 200 cap is a guardrail, not a UX limit. **The
Python port must preserve the cap-and-over-fetch pattern** or it will
regress on a previously-shipped bug.

### 3.10 Worker Polling Loop

```go
for {
    select {
    case <-ctx.Done(): return ctx.Err()
    default:
    }
    // run one iteration, then sleep interval
}
```

The pre-iteration select is critical: a cancelled context exits
immediately instead of running one more iteration. Translates to:

```python
while True:
    if cancelled: return
    await run_one_iteration()
    await asyncio.sleep(interval)
```

### 3.11 App Field Visibility (Post-2026-06 Refactor)

`App` exposes ONLY service types (`Ingest`, `Search`, `Items`, `Models`,
etc.). Raw ports (`db`, `repo`, `fs`, `searcher`, `embedder`,
`embeddingRepo`, `registry`, `project`) are **unexported**. This forces
the CLI to go through services, preserving the hexagonal boundary. If
the Python port uses module-level singletons (common pattern), it loses
this enforcement — be deliberate about it.

## 4. Cross-Cutting Concerns

### 4.1 FileStore — sha256 + Refcount

`Put(content []byte) (uri string, err error)` returns `"file://<sha256>"`.
Internally: SHA-256 hash, content-addressed storage under
`<data_dir>/filestore/<sha256[:2]>/<sha256>`. Refcount column tracks
how many items reference each blob; `Delete(uri)` decrements and
physically removes the file when refcount hits 0.

**For the Python port:** the Go schema and on-disk layout are
interoperable. The Python adapter must produce identical `file://<sha256>`
URIs and use the same `<data_dir>/filestore/<sha256[:2]>/<sha256>` path
or existing data won't be found.

### 4.2 Embedding Model Slug Threading

The active model's slug flows through: `config.yaml` → `Wire` →
`Embedder.Model().Slug` → `EmbeddingService.Embed(item, model_slug)` →
`EmbeddingRepo.Put(item_id, model_slug, vector, ...)`. The vec0 table
name (`vec_bge_m3_1024`) is derived from the model slug + dimension.
Multi-model registry (Plan 2c) lets users register additional models
and switch between them — each gets its own vec0 table.

**Python port:** mirror the `vec_table = "vec_" + slug.replace('-', '_')
+ "_" + str(dim)` derivation. The current default is `bge-m3` (slug),
1024 (dim) → `vec_bge_m3_1024`.

### 4.3 Logger Injection — Never `os.Stderr` in Services

Every service constructor takes `log io.Writer` as the last param before
variadic opts. Tests pass `io.Discard` or `*bytes.Buffer`. Production
passes `os.Stderr` from `app.Wire`. **Services have zero `os` imports**
— enforced by reviewer since the 2026-06 refactor.

The Python port should mirror this: services take `log: logging.Logger`
or `log: TextIO`, never `import sys; print(..., file=sys.stderr)` inside
service code.

### 4.4 Build/Run-Time Configuration

- Config dir: `XDG_CONFIG_HOME/unictx/config.yaml` (default
  `~/.config/unictx/config.yaml`)
- Data dir: `XDG_DATA_HOME/unictx/` (default `~/.local/share/unictx/`)
- DB at `<data_dir>/unictx.db`
- FileStore at `<data_dir>/filestore/`

`config.Load(path)` is tolerant: missing file is not an error. Defaults
apply per-type (`bge-m3`, dimension 1024, Ollama at `:11434`, etc.).

## 5. Bugs Caught + Lessons Learned

Read each of these before porting — the same bug class will recur in
Python unless you actively avoid it.

### 5.1 Externalized Content Unsearchable (2026-06-23, commit `3159020`)

AFTER INSERT trigger wrote empty FTS row for externalized content.
Search returned 0 hits even when the keyword was in the FileStore bytes.
**Fix:** `ReindexFTS` after `repo.Create`. **Side effect:** triggers
the malformed bug below.

### 5.2 Malformed FTS on Externalized Content (2026-06-26, commit `706de09`)

See §3.2 above. The 2026-06-23 fix was incomplete — it made MATCH find
rows but `snippet()` then failed on the divergence. **Spike confirms
this is language-agnostic.** The Python port must use the FIXED
`searchSQL` (no content-column snippet) from day 1.

### 5.3 Trigram 3-Char Minimum + LIKE Fallback (2026-06-23)

FTS5 trigram tokenizer indexes contiguous 3-char sequences. Queries
shorter than 3 runes (e.g. 2-char CJK `部署`) silently return 0 results.
**Fix:** LIKE `%query%` fallback for queries shorter than 3 runes,
escaped against `%`/`_` wildcards. LIKE matches get flat score 1.0,
no snippet.

**Python port:** port the `likePattern` + `likeSearchSQL` paths verbatim.
CJK support is a stated UX requirement; don't drop the LIKE fallback.

### 5.4 Hybrid Search Failure Modes (multiple commits)

- Vector leg failure must NOT abort search (`a038e1c`)
- FTS leg failure must NOT abort hybrid (`a038e1c` again)
- Per-leg timeout prevents a wedged leg from blocking the other (`b410e3e`)
- RRF rank counter must be post-filter, not pre-filter (`d85dc2b`)
- RRF tiebreak prefers newer items on score tie (`be4cfd9`)
- Over-fetch is 3× limit on FTS-only path too (`949b8f9`)

### 5.5 gxpdf API Limitations

- `OpenFromBytesWithContext(ctx, content)` not `NewReader(io.ReaderAt, size)`
- `ExtractText()` returns only string, not `(string, error)` — errors
  swallowed internally via slog
- AES-256 encrypted PDFs not supported (gxpdf V=5 limitation) — use
  RC4-40 for test fixtures
- Image-only / scanned PDFs return empty string, no error
- `doc.Close()` exists and should be called

**Python port:** swap to PyMuPDF (fitz). Better API, supports AES-256,
faster, handles image-only PDFs via OCR-ready hooks. Plan calls for
PyMuPDF, not pdfplumber.

### 5.6 Concurrent Model Register Race

`ModelRegistry.Register` from two goroutines simultaneously could both
succeed, leaving duplicates. **Fix:** recognize sqlite's UNIQUE error
specifically (not generic failure) and translate to a friendly message
(commit `ebdb3c4`). Wrap with `errors.Is`.

**Python port:** sqlite3.IntegrityError carries a similar pattern —
match on the sqlite error code, not the message.

### 5.7 CLI Test State Leaks

Package-level flag vars (`noteFilePath`, `pdfEngine`, etc.) leak between
tests unless explicitly reset. Helper `resetNoteFlags(t)` resets in both
initial block AND `t.Cleanup` (cobra flags persist after Execute).

**Python port:** Typer/click has similar global state via `click.Context`.
Use `pytest` fixtures with `autouse=True` to reset, or instantiate a
fresh `typer.Typer()` per test.

### 5.8 Doctor Status vs Reality

`doctor` command reports `status: OK` even when the embedder is
unreachable. Cosmetic UX issue; noted as deferred Minor. Don't carry
this forward into the Python port — make the status reflect actual
health checks.

### 5.9 SQLite FTS5 Build Tag

Go build needs `-tags sqlite_fts5` or FTS5 module is missing.
Custom error wrapper `wrapMigrationErr` detects "no such module: fts5"
and emits an actionable hint pointing at the build tag.

**Python port:** Python's bundled sqlite3 has FTS5 enabled by default
on macOS / Linux. Verify in spike — already confirmed working. No build
tag needed, but you may want a runtime check that emits a friendly error.

## 6. CLI Command Surface (Feature Parity Checklist)

Every command must have a Python equivalent. Flags listed are the
user-visible ones — internal plumbing differs.

### `unictx user note add`
- Positional: content text (or stdin if no positional)
- `--file <path>` — import from .txt / .md / .pdf
- `--title <t>` — title override
- `--tags a,b,c` — comma-separated tags
- `--engine gxpdf|shell|http` — PDF extractor override
- `--json` — machine-readable output
- Size cap: 50 MB
- MIME detection by extension (`.md`/`.markdown` → text/markdown,
  `.pdf` → application/pdf, else text/plain)

### `unictx user note get <id>`
Hydrates Content from FileStore if externalized. Output formats: plain,
JSON.

### `unictx user note list`
Filter by `--scope`, `--kind`, `--tags`. Paginated via cursor.

### `unictx user note delete <id>`

### `unictx search <query>`
- `--mode fts-only|hybrid` (default fts-only)
- `--limit N` (default 20, clamped 1-200)
- `--scope`, `--kind` filters
- `--json`
- LIKE fallback for queries < 3 runes
- Hybrid: RRF fusion of FTS + vector legs

### `unictx embed model list|add|remove|switch`
- `add --slug X --provider Y --dim N [--url U] [--api-key K]`
- `switch <slug>` — sets active model
- `list` — shows all registered models
- `remove <slug>` — deletes model + its vec table + cascades embeddings

### `unictx embed status`
Shows per-item embedding status across all registered models.

### `unictx embed backfill [--limit N] [--dry-run]`
Bulk-embed items lacking embeddings for the active model.

### `unictx embed worker [--interval Ns]`
Long-running poller for async embedding.

### `unictx embed reembed --model <slug> [--limit N] [--dry-run]`
Re-embeds items under a different model (typically after switch).

### `unictx reindex-fts [--limit N] [--dry-run]`
Heals FTS for externalized content. Idempotent.

### `unictx doctor`
Reports schema_version + pings the configured embedder.

## 7. Configuration Schema

```yaml
user:
  id: default             # owner_user_id for new items
data_dir: ""              # empty → XDG_DATA_HOME/unictx
embedder:
  enabled: false          # Plan 1 compat default
  provider: ollama        # or "openai"
  base_url: ""            # provider-specific default if empty
  model: bge-m3
  dimension: 1024
  api_key: ""             # OpenAI hosted; local servers ignore
pdf:
  engine: ""              # empty disables PDF support
  engines:
    shell:
      command: "pdftotext - -"
      timeout: 30s
    http:
      url: "http://localhost:8000/extract"
      timeout: 30s
      auth_token: ""
```

## 8. Gotchas the Python Port Will Hit

These are language-level traps, not architectural ones.

1. **aiosqlite runs SQLite in a worker thread.** Extension loading must
   happen on that thread. Use `await db.load_extension(...)` after
   `await db.enable_load_extension(True)`. Spike validates this works.
2. **`sqlite3.connect()` defaults to deferred FK enforcement.** Open
   with `sqlite3.connect(path, isolation_level=None)` for autocommit
   mode, OR explicitly `PRAGMA foreign_keys = ON`. Go uses
   `_foreign_keys=on` in the DSN.
3. **`aiosqlite` doesn't have `executescript` semantics identical to
   sqlite3.** Multi-statement SQL needs `await db.executescript(...)`
   OR executing each statement separately within an explicit
   transaction. The Go migration runner wraps each migration body in
   a single transaction; mirror that.
4. **Cursor pagination comparator must match Go's `(created_at < ? OR
   (created_at = ? AND id < ?))`** clause byte-for-byte, or pagination
   skips/duplicates rows at boundaries.
5. **`time.Now().Unix()` writes integer ts.** Don't write ISO 8601
   strings — that breaks `created_at` comparisons.
6. **Empty string vs NULL semantics differ.** Go's `nullable(s)` returns
   `nil` for empty strings. SQLite NULL sorts differently than `''` in
   ORDER BY. Match Go's behaviour.
7. **WAL mode on `:memory:` is silently ignored.** Cosmetic — not a
   correctness issue but trips people up in tests.
8. **`uuid.uuid7()` (Python 3.14+) replaces Go's `uuid.NewV7()`.** Both
   produce RFC 9562 UUIDv7 with embedded Unix ms timestamp. Sortable.

## 9. What to Skip on Port (Deliberate Non-Goals)

These are documented limitations or deferred items in the Go codebase.
The Python port can choose to address them, but they're not in scope
for "feature parity port":

- `--password` for encrypted PDFs
- `--pages 1-10` page-range selection
- `--no-size-limit` escape hatch
- Doctor `status: OK` not reflecting embedder FAIL state
- Migration 0002's missing schema_version bump (harmless tech debt)
- Worker/Backfill progress logging firing only on successful embeds
  (undercounts during high-failure runs)

## 10. Open Architectural Questions for the Port

Decisions the Python port implementer must make consciously:

1. **Sync vs async.** Recommendation: sync-first (matches Go's posture);
   wrap HTTP embedder calls in `asyncio.to_thread` if you want
   non-blocking. Going fully async doubles the surface area for marginal
   benefit on a single-user SQLite-bound tool.
2. **CLI framework.** Typer is fine. Click is also fine. Don't use
   argparse directly — too much boilerplate.
3. **PDF engine default.** PyMuPDF (fitz) is the recommended default —
   fast, AES-256-capable, handles image-only PDFs better than gxpdf.
4. **DB sharing during migration.** Recommend read-only Python access
   against the existing Go-written DB for verification, then a hard
   cutover. Avoid concurrent-write scenarios during migration.
5. **Test framework.** `pytest` + `pytest-asyncio` (if async). Port
   fakeRepo / cannedFileStore stubs first — they're load-bearing in
   ~30 service-layer tests.
6. **Distribution.** `uv tool install` from a published wheel, or
   `pipx install`. Accept that this is a UX regression vs single Go
   binary; consider a `homebrew` formula to ease install.

## Cross-References

- Live plan: `docs/superpowers/plans/2026-06-26-python-migration.md`
- Spike results: `spikes/python-migration/spike.py`
- Per-plan progress: `.superpowers/sdd/progress.md`
- CHANGELOG: `CHANGELOG.md` (each section documents a Plan's outcomes)
