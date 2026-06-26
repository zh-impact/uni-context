# Python Migration Plan (Revised — Modular Monolith)

> **For agentic workers:** This is a strategic migration plan, not a
> feature plan. Tasks are larger than feature TDD cycles and end with
> parity-verification gates, not unit tests. Use checkbox (`- [ ]`) syntax
> for tracking.

**Goal:** Port uni-context from Go to Python without losing user data,
feature parity, or the hard-won bugfixes baked into the Go codebase.

**Architecture:** Sync-first Python (mirrors Go's mostly-sync posture);
`asyncio` reserved for HTTP embedder calls only. **Modular monolith** —
feature-organized modules (`items/`, `search/`, `embed/`, `pdf/`,
`storage/`, `cli/`) with direct cross-module imports. NOT hexagonal
layering — that was over-engineered for a single-user SQLite-bound tool.

**Tech Stack:** Python 3.14, `sqlite3` (stdlib) + `sqlite-vec` (PyPI),
`httpx` (sync + async), `typer`, `pyyaml`, `pymupdf` (default PDF engine),
`pytest` + `pytest-asyncio`.

**Reference docs:**
- Spike: `python/spikes/migration-spike/spike.py` — 6/6 risks validated
- Go archive: `docs/superpowers/specs/2026-06-26-go-implementation-archive.md`
- Original plan: superseded by this document

## Motivation

1. **PDF library ecosystem** — gxpdf was the only勉强-usable option in Go;
   Python has PyMuPDF, pypdf, pdfminer, pdfplumber, plus native bindings.
2. **Future web/agent layer** — Python ecosystem dominates agent frameworks;
   Go is a tangent for these.
3. **Iterate faster** — REPL + hot reload > compile cycle.
4. **Author familiarity** — Python is the author's stronger language.

The Go implementation is archived (under `archive/go/`), not deleted. The
Python port is the new primary; Go remains referenceable in git history.

## Structure vs. Invariants — Read This

The Go implementation's **invariants** are preserved (PDF branch ordering,
rollback contract, embed-skip scope, RRF formula, cursor format,
malformed-FTS SQL fix — full list in Go archive §3). The Python port's
**structure** is different on purpose:

- Go: hexagonal layers (`domain/port/adapter/service/cli/app/config`)
- Python: modular monolith (`items/ search/ embed/ pdf/ storage/ cli/`)

**Structure can change; invariants shouldn't.** Every task in this plan
that touches an invariant cites it (e.g., "§3.4 rollback contract") so a
reviewer can check the Go archive spec to verify faithfulness.

## Module Structure

```
python/
├── pyproject.toml
├── src/unictx/
│   ├── __init__.py
│   ├── config.py              # Config schema + YAML loader (was: config/)
│   │
│   ├── items/                 # ContextItem + Ingest + query-side Item svc
│   │   ├── models.py          # ContextItem, Project, Scope/Kind/Source enums
│   │   ├── repo.py            # Protocol: ContextRepo (consumer-defined)
│   │   ├── ingest.py          # IngestService (CRITICAL — most invariants)
│   │   ├── item_service.py    # ItemService (get/list/delete + hydration)
│   │   └── reindex_fts.py     # ReindexFTSService (bulk FTS rebuild)
│   │
│   ├── search/                # FTS + hybrid + RRF
│   │   ├── models.py          # SearchHit, SearchQuery, SearchMode
│   │   ├── searcher.py        # Protocol: Searcher
│   │   ├── service.py         # SearchService (RRF, per-leg timeout)
│   │   └── rrf.py             # RRF formula helper (rank + 60)
│   │
│   ├── embed/                 # Embedder + model registry + workers
│   │   ├── models.py          # ModelInfo, ModelSpec, EmbeddingStatus
│   │   ├── embedder.py        # Protocol: Embedder
│   │   ├── ollama.py          # Ollama HTTP embedder
│   │   ├── openai.py          # OpenAI-compatible HTTP embedder
│   │   ├── embedding_repo.py  # Protocol: EmbeddingRepo
│   │   ├── model_registry.py  # Protocol: ModelRegistry + reconcile self-heal
│   │   ├── service.py         # EmbedService
│   │   ├── worker.py          # WorkerService (poll loop)
│   │   ├── backfill.py        # BackfillService
│   │   ├── reembed.py         # ReembedService
│   │   ├── model_service.py   # ModelService (CRUD over registry)
│   │   └── diagnostic.py      # DiagnosticService (schema + ping)
│   │
│   ├── pdf/                   # PDF extractor engines + factory
│   │   ├── extractor.py       # Protocol: PDFExtractor
│   │   ├── fitz_engine.py     # PyMuPDF (default)
│   │   ├── shell_engine.py    # subprocess wrapper
│   │   ├── http_engine.py     # POST-to-service
│   │   └── factory.py         # build_pdf_extractor(cfg, log)
│   │
│   ├── storage/               # SQLite + FileStore concrete impls
│   │   ├── db.py              # connection factory + sqlite-vec loading
│   │   ├── migrations/        # 0001-0004 .sql (verbatim from Go)
│   │   ├── migrations_runner.py
│   │   ├── repo_impl.py       # ContextRepo impl (was: adapter/sqlite/repo.go)
│   │   ├── searcher_impl.py   # Searcher impl (FIXED searchSQL)
│   │   ├── vectorstore_impl.py # VectorStore impl (vec0 KNN)
│   │   ├── embedding_repo_impl.py
│   │   ├── model_registry_impl.py
│   │   ├── schema_meta.py     # SchemaMeta (for DiagnosticService)
│   │   └── filestore.py       # sha256 content-addressed, refcounted
│   │
│   └── cli/                   # Typer commands + DI wiring
│       ├── app.py             # main Typer() + global flags + wire()
│       ├── user_note.py       # user note add/get/list/delete
│       ├── search.py          # search <query>
│       ├── embed_cmd.py       # embed model/worker/backfill/reembed/status
│       ├── doctor.py          # doctor
│       └── reindex_fts_cmd.py # reindex-fts
│
└── tests/                     # mirrors src/unictx/ structure
    ├── items/
    ├── search/
    ├── embed/
    ├── pdf/
    ├── storage/
    └── cli/
```

**Why modules own their Protocols:** Python's structural typing means
`storage/repo_impl.py` satisfies `items/repo.py:ContextRepo` without an
`implements` declaration. The consumer module defines the Protocol it
needs; the producer just matches the shape. This is the modular-monolith
idiom — dependencies point at the consumer, not a shared `port/` package.

**Why `storage/` is the producer only:** It has no Protocols of its own
(just concrete impls). All cross-module imports in services point at
`items/models.py`, `search/models.py`, etc. — never at `storage/`
internals. This keeps the SQL substrate swappable in a way the hexagonal
layout didn't.

## Binding Decisions (Locked, Not Re-Litigated)

These decisions were made in the design conversation on 2026-06-26 and
are not subject to per-task revisiting. If new information contradicts
one, escalate to a plan revision before proceeding.

- **Sync-first.** Use `sqlite3` (stdlib). Wrap HTTP embedder calls in
  `asyncio.to_thread` if non-blocking needed. Do NOT make service-layer
  methods `async def` — that doubles surface area for no gain on a
  single-user SQLite-bound tool.
- **PyMuPDF (fitz) is the default PDF engine.** NOT pdfplumber (10-50×
  slower). Keep `shell` and `http` engines for choice.
- **Python 3.14 minimum.** Uses native `uuid.uuid7()`.
- **Reuse SQL migrations verbatim.** Same `0001_init.sql` through
  `0004_embedding_model_slug_cascade.sql`. Migration runner logic
  must be ported.
- **DB file format compatible.** Python port reads existing Go-written
  DB without conversion. Cursor format must match (base36, not decimal).
- **FTS malformed bug is SQL-level.** Python port uses the FIXED
  `searchSQL` from day 1 (title-snippet only, no content-column snippet).
  See Go commit `706de09` and Go archive §3.2.
- **Modular monolith, not hexagonal.** Module-per-feature as shown above.
  No `domain/port/adapter/service/cli/app/config` layering.

## Global Constraints

These bind every task. Don't restate them in task briefs.

- Python 3.14 stdlib + listed deps only. No silent dep additions
  mid-task.
- `pytest` runs the full suite. `pytest --asyncio-mode=auto` for the
  async tests (embedder HTTP only).
- Each task ends with: (a) full suite green; (b) feature-parity
  checklist item ticked off; (c) commit with conventional commit msg.
- Every Go invariant in Go archive §3 must be preserved. Violations
  need an explicit "DEVIATION" note in the commit message with rationale.
- FileStore path layout identical: `<data_dir>/filestore/<sha256[:2]>/<sha256>`.
- Format on edit: `ruff format` on every touched `.py` file.
- DB at `~/.local/share/unictx/unictx.db` (XDG-aware; same as Go).
- Config at `~/.config/unictx/config.yaml` (XDG-aware; same as Go).
- Cross-module imports allowed: `from unictx.items.models import ...`,
  `from unictx.storage.repo_impl import ...`. Forbidden: CLI importing
  `storage/` internals directly — must go through services (matches Go's
  post-cleanup rule that landed in commit `4cfc701`).

## DB Sharing Strategy

During development, Python reads the existing Go-written DB in
**read-only mode** (`?mode=ro` URI) to validate compatibility. No
concurrent writes — SQLite WAL allows it in principle, but mixing
two write paths during active development is asking for trouble.

**Cutover plan (Phase 8):** Once Python passes feature-parity verification
on read-only data, take a backup of the Go DB, point Python at it
read-write, run a known-good set of operations, verify no corruption,
and declare Python primary. Go binary is archived.

## Phase Outline

| Phase | Scope | Est. Days | Cumulative |
|-------|-------|-----------|------------|
| 1 | Scaffolding + module skeleton + domain models + Protocols | 1.5 | 1.5 |
| 2 | `storage/` module (db, migrations, all *_impl.py) | 5 | 6.5 |
| 3 | `storage/filestore.py` + `embed/` HTTP embedders | 1.5 | 8 |
| 4 | `pdf/` module (fitz + shell + http engines + factory) | 2 | 10 |
| 5 | Services across modules (Ingest, Search, Embed, etc.) | 4 | 14 |
| 6 | `cli/` module (typer commands + wiring) | 2 | 16 |
| 7 | Test backfill (port all Go tests; ~50 cases) | 5 | 21 |
| 8 | Feature-parity verification + cutover | 1 | 22 |

**Total: ~22 working days** (4.5 weeks). Original plan said 8; that
estimate omitted test porting, realistic SQLite adapter work, and
debugging time.

---

## Phase 1 — Scaffolding + Domain Models + Protocols

**Goal:** skeleton repo, types, interfaces. Module boundaries visible.

### Task 1.1 — uv project + module skeleton

- [ ] `cd python && uv init --python 3.14 --package unictx`
  (creates `python/src/unictx/` layout)
- [ ] `uv add sqlite-vec httpx typer pyyaml pymupdf`
- [ ] `uv add --dev pytest pytest-asyncio ruff`
- [ ] Create empty package skeleton:
  - `python/src/unictx/{items,search,embed,pdf,storage,cli}/__init__.py`
  - `python/src/unictx/__init__.py`
  - `python/tests/{items,search,embed,pdf,storage,cli}/__init__.py`
  - `python/tests/__init__.py`
- [ ] Verify `uv run python -c "import sqlite_vec, httpx, typer, yaml, fitz; print('ok')"`
- [ ] Verify `uv run pytest` collects zero tests cleanly
- [ ] Commit: `feat: init python project with module skeleton + deps`

### Task 1.2 — `items/models.py` — domain types

Port `internal/domain/context.go` and `internal/domain/project.go`:
- [ ] `ContextItem` dataclass with all fields (id, scope, kind, source,
  owner_user_id, title, summary, content, content_uri, content_mime,
  tags, source_meta, word_count, created_at, updated_at, version, ...)
- [ ] `Scope`, `Kind`, `Source` as `StrEnum` (Python 3.11+; matches Go's
  string-backed enums)
- [ ] `Project` dataclass
- [ ] `NewItemParams` + `NewContextItem` factory with
  `validateCombination` logic
- [ ] `ContentInlineLimit = 4 * 1024` constant
- [ ] `countWords` function (port Go's implementation; note Minor: Go's
  undercounts CJK — preserve for parity, don't "fix" without a separate
  discussion)
- [ ] Unit tests for `validateCombination` + `NewContextItem` in
  `tests/items/test_models.py`
- [ ] Commit: `feat(items): port ContextItem + Project + enums`

### Task 1.3 — Distributed Protocols across modules

Define `Protocol` interfaces in the consuming module, not a shared `port/`:

- [ ] `items/repo.py` — `ContextRepo` Protocol (get/list/create/update/
  delete/reindex_fts) + `ItemFilter` dataclass
- [ ] `search/searcher.py` — `Searcher` Protocol + `SearchHit`,
  `SearchQuery`, `SearchMode` dataclasses/enums
- [ ] `search/service.py` — placeholder import target (impl in Phase 5)
- [ ] `embed/embedder.py` — `Embedder` Protocol + `ModelInfo` dataclass
- [ ] `embed/embedding_repo.py` — `EmbeddingRepo` Protocol +
  `EmbeddingStatus` dataclass
- [ ] `embed/model_registry.py` — `ModelRegistry` Protocol + `ModelSpec`
  dataclass
- [ ] `storage/filestore.py` — `FileStore` Protocol (defined here because
  storage/ owns the impl too; see Module Structure note above)
- [ ] `pdf/extractor.py` — `PDFExtractor` Protocol
- [ ] All as `@runtime_checkable Protocol` with method signatures
  matching Go's `port/*.go`. Drop `ctx context.Context` params — sync
  Python doesn't need them. (Async services would use `asyncio.CancelledError`.)
- [ ] Tests: structural-typing smoke tests (a stub class satisfies each
  Protocol via `isinstance` check with `@runtime_checkable`).
- [ ] Commit: `feat: define Protocol interfaces across modules`

---

## Phase 2 — `storage/` Module (the big one)

**Goal:** all four migrations applied; `*_impl.py` working against
`:memory:` AND the existing Go-written DB.

### Task 2.1 — Connection factory + extension loading

- [ ] `storage/db.py` — `open_db(path, *, read_only=False)` opens
  the connection, loads `sqlite_vec` extension, enables foreign_keys
  pragma, sets isolation_level appropriately.
- [ ] Test: `open_db(":memory:")` + `select vec_version()` works
- [ ] Test: `open_db(<existing Go DB>, read_only=True)` reads schema_meta
- [ ] Commit: `feat(storage): db connection factory with sqlite-vec loading`

### Task 2.2 — Migration runner

Port `internal/adapter/sqlite/migrations.go`:
- [ ] Copy `migrations/*.sql` files verbatim from Go archive
  (`archive/go/internal/adapter/sqlite/migrations/`) →
  `python/src/unictx/storage/migrations/`.
- [ ] Embed via `importlib.resources` (Python equivalent of Go's `embed.FS`)
- [ ] Parse version from filename (`NNNN_*.sql`)
- [ ] `migrate(db)` applies pending migrations in order
- [ ] Each migration wrapped in a transaction (Python: explicit BEGIN/COMMIT
  OR `db.isolation_level=None` + manual BEGIN)
- [ ] `wrap_migration_err` equivalent: detect "no such module: fts5"
  and emit actionable hint (Python's bundled sqlite has FTS5 by default
  but be defensive)
- [ ] Tests: `migrate(:memory:)` applies all 4; idempotent; expected
  schema_version='4'
- [ ] Commit: `feat(storage): migration runner with FTS5 error hint`

### Task 2.3 — `storage/repo_impl.py` — ContextRepo impl

Port `internal/adapter/sqlite/repo.go`:
- [ ] `create(item)` — INSERT, AFTER INSERT trigger writes FTS row
- [ ] `get(id)` — SELECT; return `NotFoundError` if missing
- [ ] `update(item)` — UPDATE with version increment; AFTER UPDATE
  trigger rewrites FTS row
- [ ] `delete(id)` — DELETE; AFTER DELETE trigger removes FTS row
- [ ] `list(filter)` — paginated with cursor (base36 ts + ":" + id format)
- [ ] `reindex_fts(id, title, summary, content)` — direct INSERT into
  context_fts, bypassing the trigger pair
- [ ] `encode_cursor(ts, id)` / `decode_cursor(c)` — base36, byte-identical
  to Go's `strconv.FormatInt(ts, 36)`. Spike has the verified impl.
- [ ] `scan_item` helper — JSON decode tags + source_meta, handle NULL
- [ ] Tests: roundtrip create/get/update/delete; list pagination;
  reindex_fts for externalized; cursor format cross-checked with Go
- [ ] Commit: `feat(storage): ContextRepo impl with base36 cursor + reindex_fts`

### Task 2.4 — `storage/searcher_impl.py` — Searcher impl

Port `internal/adapter/sqlite/searcher.go`:
- [ ] `search_fts(query)` — uses FIXED searchSQL (title-snippet only;
  NO content-column snippet — see Go archive §3.2)
- [ ] LIKE fallback for queries < 3 runes (`like_search(query)`)
- [ ] `clamp_limit(n)` — same semantics as Go (<=0 → 20, >200 → 200,
  else unchanged). Preserve the bug-fix from commit `4d26cea`.
- [ ] `fts_query_string(raw)` — wrap in `"..."` with embedded quotes
  doubled (FTS5 phrase query, prevents operator injection)
- [ ] `like_pattern(raw)` — escape `%`, `_`, `\` for literal LIKE match
- [ ] Tests: basic FTS match; CJK trigram; BM25 ranking; no match;
  empty query; injection safety; LIKE fallback for short queries;
  LIKE wildcard escaping; clamp limit regression
- [ ] **Regression test (CRITICAL):** externalized content does not
  corrupt — port `TestSearcher_FTS_ExternalizedContentDoesNotCorrupt`
  from Go test suite. Use the same setup (ReindexFTS with content
  bypassing triggers).
- [ ] Commit: `feat(storage): Searcher impl with FTS + LIKE fallback`

### Task 2.5 — `storage/vectorstore_impl.py`

- [ ] `search(vector, model_slug, limit)` — JOIN vec0 virtual table
  with context_item, KNN match
- [ ] K=200 internal cap (matches Go)
- [ ] Same clamp_limit semantics
- [ ] Tests: KNN finds embedded items; dimension mismatch handled;
  limit clamp
- [ ] Commit: `feat(storage): VectorStore impl using sqlite-vec`

### Task 2.6 — `storage/embedding_repo_impl.py`

- [ ] `put(item_id, model_slug, vector, status)` — UPSERT (DELETE+INSERT
  in tx, matching Go's approach since vec0 doesn't support INSERT OR REPLACE)
- [ ] `get(item_id, model_slug)`
- [ ] `list_for_item(item_id)` — all models' status
- [ ] `delete(item_id, model_slug)`
- [ ] Tests: put/get roundtrip; status transitions; cascade on item delete
- [ ] Commit: `feat(storage): EmbeddingRepo impl with UPSERT semantics`

### Task 2.7 — `storage/model_registry_impl.py` + `storage/schema_meta.py`

- [ ] `register(spec)` — INSERT, detect UNIQUE-violation → friendly error
- [ ] `list()` — SELECT all, ordered
- [ ] `remove(slug)` — DELETE model + cascade via FK 0004 (or manual
  DELETE if cascade doesn't fire) + DROP vec0 table
- [ ] `set_default(slug)` — transactional UPDATE
- [ ] `scan_model(config_row)` — error class for corrupt config
- [ ] `reconcile_plan2c_sync(db)` — idempotent self-heal on startup
- [ ] `vec_table_name(slug, dim)` — `"vec_" + slug.replace('-', '_') + "_" + str(dim)`
- [ ] `schema_meta.py` — `version(ctx)` queries `schema_meta` table
- [ ] Tests: register/list/remove; default switch; shared-vec-table refusal
- [ ] Commit: `feat(storage): ModelRegistry impl + SchemaMeta`

---

## Phase 3 — `storage/filestore.py` + `embed/` HTTP embedders

### Task 3.1 — `storage/filestore.py`

- [ ] sha256-addressed, refcounted (single module: Protocol + impl
  co-located, since storage/ owns both)
- [ ] `put(content: bytes) -> str` — returns `"file://<sha256>"`
- [ ] `get(uri: str) -> bytes`
- [ ] `delete(uri: str)` — decrement refcount, remove file when 0
- [ ] Path layout identical to Go: `<data_dir>/filestore/<sha256[:2]>/<sha256>`
- [ ] Tests: put/get roundtrip; delete with refcount>0 keeps file;
  delete to 0 removes; cross-compatible with Go-written FileStore
  (read a Go-written blob, verify content)
- [ ] Commit: `feat(storage): sha256 content-addressed file store`

### Task 3.2 — `embed/ollama.py`

- [ ] Sync httpx client
- [ ] `embed(texts: list[str]) -> list[list[float]]` — POST to /api/embed
- [ ] `model() -> ModelInfo`
- [ ] 30s default timeout
- [ ] Tests: against a mock server (use `httpx.MockTransport`); embed
  returns correct dims; model info correct
- [ ] Commit: `feat(embed): Ollama embedder`

### Task 3.3 — `embed/openai.py`

- [ ] For LMStudio, vLLM, hosted OpenAI
- [ ] `embed(texts)` — POST to /v1/embeddings; optional Bearer auth
- [ ] Tests: same structure as Ollama; auth header omitted when api_key empty
- [ ] Commit: `feat(embed): OpenAI-compatible embedder`

---

## Phase 4 — `pdf/` Module

### Task 4.1 — `pdf/fitz_engine.py` (default)

- [ ] Implements `pdf/extractor.py:PDFExtractor` Protocol
- [ ] `extract(content: bytes) -> str` — open from bytes, iterate pages,
  accumulate text
- [ ] Encrypted PDFs raise with "encrypted pdf" in message
- [ ] Image-only PDFs return "" (no error)
- [ ] Tests: real PDF fixtures (copy from
  `archive/go/internal/adapter/pdf/testdata/`); encrypted; image-only;
  multi-page
- [ ] Commit: `feat(pdf): PyMuPDF engine as default`

### Task 4.2 — `pdf/shell_engine.py`

- [ ] subprocess wrapper
- [ ] 30s default timeout
- [ ] Detect "command not found" cleanly (`cmd.ProcessState == nil` equivalent)
- [ ] Tests: working command; timeout; non-zero exit; command not found
- [ ] Commit: `feat(pdf): shell engine for external commands`

### Task 4.3 — `pdf/http_engine.py`

- [ ] POST binary to a service
- [ ] 30s default timeout; optional Bearer auth
- [ ] Require text/plain response
- [ ] Tests: mocked HTTP server; timeout; auth; non-200; wrong content-type
- [ ] Commit: `feat(pdf): http engine for service-based extraction`

### Task 4.4 — `pdf/factory.py`

- [ ] `build_pdf_extractor(cfg, log)`, `build_extractor_for_engine(engine, cfg, log)`
- [ ] `(nil, None)` equivalent: returns `(None, None)` when engine is empty
- [ ] Per-engine validation
- [ ] Tests: disabled → none; enabled with each engine; misconfigured → error
- [ ] Commit: `feat(pdf): extractor factory`

---

## Phase 5 — Services Across Modules

This is where invariants get load-bearing. Read Go archive §3 before
each task.

### Task 5.1 — `items/ingest.py` — IngestService (CRITICAL — most invariant-dense)

Port `internal/service/ingest.go`:
- [ ] Constructor: `IngestService(repo, fs, log, *opts)` — variadic via
  default-None kwargs
- [ ] `with_pdf_extractor(ext)` option (constructor-level)
- [ ] `create(input, *create_opts) -> str` — returns item ID
- [ ] `with_extractor(ext)` create-option (per-call)
- [ ] **PDF branch ordering (Go archive §3.3):** PDF extraction runs
  BEFORE `NewContextItem`. Load-bearing — do not reorder.
- [ ] **Rollback contract (§3.4):** on repo.create failure, fs.delete
  BOTH `content_uri` AND `pdf_uri` (SourceMeta["original_uri"]).
- [ ] **Embed-skip scope (§3.5):** `pdf_uri != "" and not item.content
  and not item.content_uri` → skip embedding.
- [ ] Externalize if content > ContentInlineLimit (4 KB)
- [ ] ReindexFTS after repo.create when content was externalized
- [ ] Tests: 7 PDF tests from Go (extract + store; propagates error;
  empty extraction; rollback on repo failure; embed skip; per-call
  extractor override; ...); plus all non-PDF ingest tests
- [ ] Commit: `feat(items): IngestService with PDF branch + rollback`

### Task 5.2 — `search/service.py` — SearchService

- [ ] Constructor: `SearchService(searcher, repo, embedder=None, log=...)`
- [ ] `search(request) -> response` — mode: fts-only | hybrid
- [ ] **Per-leg timeout (§3.7):** vector leg with `concurrent.futures`
  timeout; on failure/timeout, fall back to fts-only with warning
- [ ] **RRF formula (§3.8):** `Σ 1/(rank + 60)`, rank is post-filter
- [ ] **Over-fetch (§3.9):** 3× user limit on both legs; clamp to 200
- [ ] RRF tiebreak prefers newer items on score tie
- [ ] Tests: fts-only basic; hybrid basic; vector failure degrades to fts;
  fts failure continues with vector; per-leg timeout; ranking; tiebreak
- [ ] Commit: `feat(search): SearchService with RRF + per-leg timeout`

### Task 5.3 — `embed/service.py` — EmbedService

- [ ] `embed_item(item, model_slug)` — hydrate content if externalized
  (mirror Go's `hydrateContent`), call embedder, put via EmbeddingRepo
- [ ] Status row always written (success='done', failure='failed' with
  last_error, attempts counter)
- [ ] Tests: happy path; empty content; embedder failure; status row on
  all paths
- [ ] Commit: `feat(embed): EmbedService with status tracking`

### Task 5.4 — `embed/worker.py` + `embed/backfill.py` + `embed/reembed.py`

- [ ] `WorkerService.run(interval)` — poll loop with pre-iteration
  cancellation check (§3.10)
- [ ] `BackfillService.run(limit, dry_run)` — bulk embed missing
- [ ] `ReembedService.run(model, limit, dry_run)` — re-embed under
  different model
- [ ] All three: warn-and-continue on per-item failure
- [ ] Tests: worker iteration; backfill with various filters; reembed
  against different model
- [ ] Commit: `feat(embed): Worker/Backfill/Reembed async embed runners`

### Task 5.5 — `items/reindex_fts.py` — ReindexFTSService

- [ ] `run(limit, dry_run)` — walks items, hydrates externalized content
  from FileStore, calls repo.reindex_fts
- [ ] Idempotent (ReindexFTS uses delete-then-insert)
- [ ] Tests: bulk reindex; dry run; per-item failure continues
- [ ] Commit: `feat(items): ReindexFTSService bulk runner`

### Task 5.6 — `items/item_service.py` + `embed/model_service.py` + `embed/diagnostic.py`

- [ ] `ItemService` (get/list/delete) with FileStore hydration in get
- [ ] `DiagnosticService` (schema_version, ping_embedder)
- [ ] `ModelService` (add/list/remove/switch/status)
- [ ] Tests: get hydrates externalized; doctor reports correctly;
  model lifecycle
- [ ] Commit: `feat: Item/Model/Diagnostic services`

---

## Phase 6 — `cli/` Module

### Task 6.1 — Typer app skeleton + global flags + wiring

- [ ] `cli/app.py` — main `typer.Typer()`
- [ ] Global flags: `--config`, `--json`, `--verbose`
- [ ] Config loading via `unictx.config.load(path)`
- [ ] App wiring: `wire(cfg)` returns a container with all services
  (the only place that imports `storage/*_impl.py` directly — everything
  else goes through service Protocols)
- [ ] Commit: `feat(cli): typer skeleton with global flags + wiring`

### Task 6.2 — `cli/user_note.py` — `user note` subcommands

- [ ] `add` (with `--file`, `--title`, `--tags`, `--engine`)
- [ ] `get <id>` (with FileStore hydration)
- [ ] `list` (paginated, with `--scope`, `--kind`, `--tags`)
- [ ] `delete <id>`
- [ ] `--json` output format across all subcommands
- [ ] Reset state between pytest cases via fixture (Go archive §5.7)
- [ ] Tests: integration tests for each subcommand; flag parsing;
  file import; PDF with engine override
- [ ] Commit: `feat(cli): user note subcommands with --file and --engine`

### Task 6.3 — `cli/search.py` — `search` command

- [ ] `search <query>` with `--mode`, `--limit`, `--scope`, `--kind`
- [ ] LIKE fallback transparent (no user-visible difference)
- [ ] Tests: fts-only search; hybrid search; limit clamp; filters
- [ ] Commit: `feat(cli): search command with fts/hybrid modes`

### Task 6.4 — `cli/embed_cmd.py` — `embed` subcommands

- [ ] `embed model list|add|remove|switch`
- [ ] `embed status`
- [ ] `embed backfill [--limit] [--dry-run]`
- [ ] `embed worker [--interval]`
- [ ] `embed reembed --model X`
- [ ] Tests: each subcommand; switch warning; reembed signal handling
- [ ] Commit: `feat(cli): embed model + worker + backfill commands`

### Task 6.5 — `cli/doctor.py` + `cli/reindex_fts_cmd.py`

- [ ] `doctor` — schema_version + ping embedder
- [ ] `reindex-fts [--limit] [--dry-run]`
- [ ] Tests: doctor disabled embedder; doctor enabled + reachable;
  reindex-fts output
- [ ] Commit: `feat(cli): doctor and reindex-fts commands`

---

## Phase 7 — Test Backfill (the long tail)

Port remaining Go tests not yet ported in earlier phases. Estimated
~50 test cases across all modules.

- [ ] Service-layer tests using fakeRepo / cannedFileStore stubs (port
  these stubs first; they're load-bearing in ~30 tests). Stub location:
  `tests/conftest.py` (shared pytest fixtures).
- [ ] CLI integration tests (`typer.testing.CliRunner` pattern)
- [ ] Edge-case tests: concurrent model register race (Go §5.6); empty
  extraction; encrypted PDF; image-only PDF; size cap boundary
- [ ] Migration test: idempotency on fresh + migrated DB
- [ ] Commit per module or per major test group

---

## Phase 8 — Feature-Parity Verification + Cutover

### Task 8.1 — Read-only verification on existing Go DB

- [ ] Run Python CLI against existing Go DB (read-only)
- [ ] Verify each command produces equivalent output to Go binary
- [ ] Document any output format differences (intentional changes only)
- [ ] Commit: `docs: parity verification report`

### Task 8.2 — Backup + cutover

- [ ] Backup `unictx.db` + `filestore/` to `~/backups/unictx-<date>/`
- [ ] Point Python at the DB read-write
- [ ] Run a known-good sequence: add note, search, embed, doctor
- [ ] Verify no DB corruption (PRAGMA integrity_check; FTS5 integrity-check)
- [ ] Declare Python primary; archive Go binary
- [ ] Update README / install instructions
- [ ] Commit: `docs: cutover to Python implementation`

---

## Acceptance Criteria

The migration is complete when ALL of:

1. All Phase 1-7 tasks committed.
2. Full pytest suite green.
3. Python CLI passes feature-parity verification against the existing
   Go DB (Task 8.1).
4. The 11 Go invariants in Go archive §3 are preserved (or explicitly
   documented as deviations with rationale).
5. Cutover completed (Task 8.2) — Go binary archived, Python primary.
6. CHANGELOG entry summarizing the migration.

## Risk Register

Revisit at end of each phase.

| Risk | Mitigation |
|------|------------|
| sqlite-vec + aiosqlite extension loading fragility | Already spike-validated (sync + async both work) |
| FTS5 malformed bug recurs in Python | Use fixed SQL from day 1 (Go archive §3.2); regression test in Task 2.4 |
| Cursor format incompatibility breaks pagination | Spike-validated byte-identical round-trip |
| PyMuPDF wheel availability on user's platform | PyMuPDF has broad wheel coverage; verify on first install |
| Async creep inflates surface area | Sync-first mandate; only embedder HTTP calls async |
| Test porting underestimates | Phase 7 budgeted 5 full days (~50 cases) |
| PDF perf regression (any engine slower than gxpdf) | PyMuPDF benchmark vs gxpdf in Task 4.1; abort + reconsider if >2× slower |
| User data loss during cutover | Mandatory backup before write-enable |
| Concurrent Go+Python writes during dev | Read-only Python during dev (URI `?mode=ro`) |
| Doctor `status: OK` lying about embedder failure (Go §5.8) | Fix in Python port — make status reflect reality |
| Module boundary erosion (cli reaching into storage) | Linter rule (ruff import-restrictions) + code review |

## Out of Scope

Explicitly NOT in this migration:

- New features (agent framework, web UI, etc.) — those are separate
  plans once Python is primary.
- Schema redesign. The schema is identical.
- Migration of git history (the Go code stays in git as historical
  reference).
- Performance optimization beyond "not worse than Go".
- Windows support (Go version didn't have it either).
- `--password` / `--pages` / `--no-size-limit` flags (documented
  non-goals in Go archive §9).

## References

- Original migration plan (superseded): user's first message in this
  conversation, 2026-06-26
- Spike results: `python/spikes/migration-spike/spike.py` (run with
  `uv run python spike.py`)
- Go archive: `docs/superpowers/specs/2026-06-26-go-implementation-archive.md`
- Per-plan progress ledger (to be created at start of execution):
  `.superpowers/sdd/progress.md` (append new section)
