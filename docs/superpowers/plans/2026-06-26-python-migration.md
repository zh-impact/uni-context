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
`httpx` (sync + async), `typer`, `pydantic` v2 (config validation),
`pyyaml`, `pymupdf` (default PDF engine), `pytest` + `pytest-asyncio`.

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
├── pyproject.toml             # ruff config lives here too ([tool.ruff] §)
├── src/unictx/
│   ├── __init__.py            # EMPTY — no re-export (see Conventions)
│   ├── errors.py              # UnictxError base only; specifics live in modules
│   ├── config.py              # Pydantic Config schema + YAML loader
│   │
│   ├── items/                 # ContextItem + Ingest + query-side Item svc
│   │   ├── errors.py          # ItemNotFound, ExternalizedContentMissing
│   │   ├── models.py          # ContextItem, Project, Scope/Kind/Source enums
│   │   ├── repo.py            # Protocol: ContextRepo (consumer-defined)
│   │   ├── ingest.py          # IngestService (CRITICAL — most invariants)
│   │   ├── item_service.py    # ItemService (get/list/delete + hydration)
│   │   └── reindex_fts.py     # ReindexFTSService (bulk FTS rebuild)
│   │
│   ├── search/                # FTS + hybrid + RRF
│   │   ├── models.py          # SearchHit, SearchQuery, SearchMode
│   │   ├── searcher.py        # Protocol: Searcher (FTS+LIKE)
│   │   ├── vectorstore.py     # Protocol: VectorStore (vec0 KNN)
│   │   ├── service.py         # SearchService (RRF, per-leg timeout)
│   │   └── rrf.py             # RRF formula helper (rank + 60)
│   │
│   ├── embed/                 # Embedder + model registry + workers
│   │   ├── errors.py          # ModelNotFound, ModelConflict, EmbeddingFailed, StatusNotFound
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
│   │   ├── errors.py          # PDFEncrypted, PDFExtractionFailed, PDFCommandNotFound
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
│   │   ├── row_factory.py     # scan_item(cursor, row) — registered as db.row_factory
│   │   ├── schema_meta.py     # SchemaMeta (for DiagnosticService)
│   │   └── filestore.py       # sha256 content-addressed, refcounted
│   │
│   └── cli/                   # Typer commands + DI wiring
│       ├── app.py             # main Typer() + global flags + wire()
│       ├── user_note.py       # user note add/get/list/delete
│       ├── search.py          # search <query>
│       ├── embed_cmd.py       # embed model/worker/backfill/reembed/status
│       ├── doctor.py          # doctor
│       ├── reindex_fts_cmd.py # reindex-fts
│       └── output.py          # print_json(result) shared helper (mirrors Go printJSON)
│
└── tests/
    ├── conftest.py            # @pytest.fixture wrappers around _fakes/
    ├── _fakes/                # Pure stub classes, importable across test modules
    │   ├── __init__.py
    │   ├── fake_repo.py       # FakeContextRepo (in-memory)
    │   ├── canned_filestore.py
    │   └── fake_embedder.py
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

## Python Conventions (Locked)

These are project-wide code conventions. Every task inherits them.

- **`__init__.py` files stay empty.** No `from .models import *` or
  re-exports. Import paths are always explicit
  (`from unictx.items.models import ContextItem`). Rationale: modular
  monolith's main benefit is greppable, self-documenting imports.
  Re-exports make import paths ambiguous.
- **Domain models use `@dataclass(slots=True)`.** Memory-efficient for
  25-field ContextItem. NOT frozen (version increment, status updates
  mutate fields).
- **Config uses Pydantic v2 `BaseModel`.** Nested config (embedder.*,
  pdf.engines.shell.*, pdf.engines.http.*) gets free validation + type
  coercion. Domain models stay plain dataclass — only Config is Pydantic.
- **Timestamps written to DB are `int(datetime.now(timezone.utc).timestamp())`.**
  Matches Go's `time.Now().Unix()` byte-for-byte. No naive `datetime.now()`.
- **Exception hierarchy: hybrid.** `unictx/errors.py` defines only
  `UnictxError(Exception)`. Each module owns its specific exceptions
  (`items/errors.py:ItemNotFound`, `embed/errors.py:ModelNotFound`,
  `pdf/errors.py:PDFEncrypted`). All inherit from `UnictxError` so CLI
  can `except UnictxError` as a catch-all. sqlite3 UNIQUE violations
  are caught in storage/ and re-raised as the appropriate ConflictError.
- **SQLite row mapping: custom `scan_item` factory.** Register via
  `db.row_factory = scan_item` in `storage/db.py`. Returns `ContextItem`
  directly. Replaces Go's per-method `scan_item` helper; keeps query
  code SQL-focused.
- **CLI JSON output: shared `cli/output.py:print_json(result)`.**
  Every `--json` flag routes through this single-purpose helper.
  Non-JSON output is rendered per-command with rich tables or plain
  print — `print_json` does NOT branch.
- **Import restrictions enforced by ruff.** `cli/*` CANNOT import
  `storage/*_impl.py` directly (must go through services). Configured
  via `[tool.ruff.lint.per-file-ignores]` or `import-restrictions` rule.

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
- **Pydantic v2 for Config only.** Domain models (ContextItem, etc.) stay
  `@dataclass(slots=True)`. Pydantic is pulled in for Config schema
  validation only — not as a global ORM layer.
- **Hybrid exception hierarchy.** Shared `UnictxError` base in
  `errors.py`; specifics per-module. See Python Conventions §.

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
  post-cleanup rule that landed in commit `4cfc701`). Enforced by
  ruff config + guard test (see Task 1.1).
- All Python Conventions (§) bind every task: empty `__init__.py`,
  `slots=True` dataclasses, Pydantic for Config, UTC timestamps,
  hybrid exception hierarchy, `scan_item` row factory, shared output
  formatter, ruff import-restrictions.

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
| 1 | Scaffolding + module skeleton + errors + Config + models + Protocols + fixtures | 2.5 | 2.5 |
| 2 | `storage/` module (db, migrations, all *_impl.py) | 4.5 | 7 |
| 3 | `storage/filestore.py` + `embed/` HTTP embedders | 1.5 | 8.5 |
| 4 | `pdf/` module (fitz + shell + http engines + factory) | 2 | 10.5 |
| 5 | Services across modules (Ingest, Search, Embed, etc.) | 3.5 | 14 |
| 6 | `cli/` module (typer commands + wiring + output) | 2 | 16 |
| 7 | Test backfill (port all Go tests; ~50 cases) | 5 | 21 |
| 8 | Feature-parity verification + cutover | 1 | 22 |

**Total: ~22 working days** (4.5 weeks). Original plan said 8; that
estimate omitted test porting, realistic SQLite adapter work, and
debugging time.

---

## Phase 1 — Scaffolding + Domain Models + Errors + Config + Protocols + Fixtures

**Goal:** skeleton repo, types, interfaces, error base, config loader,
shared test fixtures. Every later phase depends on this.

### Task 1.1 — uv project + module skeleton + ruff config

- [ ] `cd python && uv init --python 3.14 --package unictx`
  (creates `python/src/unictx/` layout)
- [ ] `uv add sqlite-vec httpx typer pydantic pyyaml pymupdf`
- [ ] `uv add --dev pytest pytest-asyncio ruff`
- [ ] Create empty package skeleton (all `__init__.py` files empty —
  see Python Conventions §):
  - `python/src/unictx/{items,search,embed,pdf,storage,cli}/__init__.py`
  - `python/src/unictx/__init__.py`
  - `python/tests/{items,search,embed,pdf,storage,cli}/__init__.py`
  - `python/tests/__init__.py`
  - `python/tests/_fakes/__init__.py`
- [ ] Verify `uv run python -c "import sqlite_vec, httpx, typer, pydantic, yaml, fitz; print('ok')"`
- [ ] Verify `uv run pytest` collects zero tests cleanly
- [ ] Add `[tool.ruff]` config to `pyproject.toml`:
  ```toml
  [tool.ruff]
  line-length = 100
  target-version = "py314"

  [tool.ruff.lint]
  select = ["E", "W", "F", "I", "UP", "B", "SIM", "PT"]

  [tool.ruff.lint.isort]
  known-first-party = ["unictx"]

  [tool.ruff.lint.per-file-ignores]
  # CLI must NOT import storage impls directly — go through services
  "src/unictx/cli/*" = ["PT"]
  # Use TID252 (banned-api) once stable; for now document the rule in
  # reviews and rely on the test in tests/cli/test_no_direct_storage_import.py
  ```
- [ ] Add a guard test `tests/cli/test_no_direct_storage_import.py` that
  greps `cli/*.py` for `from unictx.storage.*_impl` and fails if found.
- [ ] Commit: `feat: init python project with module skeleton + ruff config`

### Task 1.2 — `unictx/errors.py` — shared exception base

- [ ] `errors.py` with single class:
  ```python
  class UnictxError(Exception):
      """Base for all uni-context domain errors.

      Specific errors live in their owning module:
      items/errors.py:ItemNotFound, embed/errors.py:ModelNotFound, etc.
      Catch UnictxError in CLI for unified error reporting.
      """
  ```
- [ ] No specifics here — they belong to the module that raises them.
- [ ] Test: trivial `isinstance(SomeModuleError(), UnictxError)` smoke
  test (added in 1.3/1.4 when specifics exist).
- [ ] Commit: `feat(errors): add UnictxError base class`

### Task 1.3 — `items/models.py` + `items/errors.py` — domain types

Port `internal/domain/context.go` and `internal/domain/project.go`:
- [ ] `ContextItem` as `@dataclass(slots=True)` with all fields (id,
  scope, kind, source, owner_user_id, title, summary, content,
  content_uri, content_mime, tags, source_meta, word_count, created_at,
  updated_at, version, ...)
- [ ] `Scope`, `Kind`, `Source` as `StrEnum` (Python 3.11+; matches Go's
  string-backed enums)
- [ ] `Project` dataclass (also `slots=True`)
- [ ] `NewItemParams` + `NewContextItem` factory with
  `validateCombination` logic
- [ ] `ContentInlineLimit = 4 * 1024` constant
- [ ] `countWords` function (port Go's implementation; note Minor: Go's
  undercounts CJK — preserve for parity, don't "fix" without a separate
  discussion)
- [ ] `items/errors.py`:
  ```python
  from unictx.errors import UnictxError

  class ItemNotFound(UnictxError):
      def __init__(self, item_id: str):
          super().__init__(f"item not found: {item_id}")
          self.item_id = item_id

  class ExternalizedContentMissing(UnictxError):
      """content_uri set but FileStore has no blob."""
      def __init__(self, uri: str):
          super().__init__(f"externalized content missing: {uri}")
          self.uri = uri
  ```
- [ ] Unit tests for `validateCombination` + `NewContextItem` + error
  subclasses in `tests/items/test_models.py` + `tests/items/test_errors.py`
- [ ] Commit: `feat(items): port ContextItem + Project + enums + errors`

### Task 1.4 — `config.py` — Pydantic Config + YAML loader + XDG

- [ ] Pydantic v2 models mirroring Go's config schema:
  ```python
  from pathlib import Path
  from pydantic import BaseModel, Field, model_validator

  class UserConfig(BaseModel):
      """Owner identity for new items. Default 'default'."""
      id: str = "default"

  class EmbedderConfig(BaseModel):
      """Controls optional embedding pipeline.

      When enabled=False (the default), the app behaves as Plan 1:
      no vector indexing, search defaults to fts-only. When enabled=True,
      apply_defaults() fills provider/base_url/model/dimension if empty
      (mirrors Go config.go:101-123).
      """
      enabled: bool = False        # ← wire() branches on this
      provider: str = ""           # "", "ollama", "openai-compat"
      base_url: str = ""
      model: str = ""
      dimension: int = 0
      api_key: str = ""            # OpenAI hosted; local servers ignore

      @model_validator(mode="after")
      def apply_defaults(self):
          if not self.enabled:
              return self
          if self.provider == "":
              self.provider = "ollama"
          if self.base_url == "":
              self.base_url = {
                  "ollama": "http://localhost:11434",
                  "openai-compat": "http://localhost:1234/v1",
              }.get(self.provider, "")
          if self.model == "":
              self.model = "bge-m3"
          if self.dimension == 0:
              self.dimension = 1024
          return self

  class ShellPdfEngineConfig(BaseModel):
      command: str = "pdftotext - -"
      timeout_seconds: int = 30

  class HttpPdfEngineConfig(BaseModel):
      url: str = "http://localhost:8000/extract"
      timeout_seconds: int = 30
      auth_token: str = ""

  class PdfEnginesConfig(BaseModel):
      """Mirrors Go's nested struct — one field per engine.

      Using a heterogeneous dict[str, A | B] would let Pydantic silently
      fall back between types on mismatch, hiding config errors. A flat
      struct validates each sub-config precisely.
      """
      shell: ShellPdfEngineConfig = Field(default_factory=ShellPdfEngineConfig)
      http: HttpPdfEngineConfig = Field(default_factory=HttpPdfEngineConfig)

  class PdfConfig(BaseModel):
      engine: str = ""             # "", "fitz", "shell", "http"
      engines: PdfEnginesConfig = Field(default_factory=PdfEnginesConfig)

  class Config(BaseModel):
      user: UserConfig = Field(default_factory=UserConfig)
      data_dir: Path = Field(default_factory=lambda: xdg_data_home() / "unictx")
      embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
      pdf: PdfConfig = Field(default_factory=PdfConfig)
  ```
  `data_dir` has a `default_factory` so `Config.model_validate({})` works
  even when YAML omits the field. `xdg_data_home()` is the module-level
  helper that resolves `$XDG_DATA_HOME` → `~/.local/share`.
- [ ] `load(path: Path | None) -> Config`:
  - Resolve path via XDG (`$XDG_CONFIG_HOME/unictx/config.yaml` →
    `~/.config/unictx/config.yaml`). Same logic as Go.
  - If file missing, return `Config(data_dir=xdg_data_home()/"unictx")`.
  - If present, `yaml.safe_load` + `Config.model_validate(data)`.
  - Pydantic raises `ValidationError` on bad shape — surface cleanly.
- [ ] `xdg_data_home()` + `xdg_config_home()` helpers (was Go's config
  defaults).
- [ ] Tests: missing file → defaults; minimal YAML; full YAML; invalid
  field → ValidationError; XDG env var honored.
- [ ] Commit: `feat(config): Pydantic Config schema + YAML loader`

### Task 1.5 — Distributed Protocols across modules

Define `Protocol` interfaces in the consuming module, not a shared `port/`:

- [ ] `items/repo.py` — `ContextRepo` Protocol (get/list/create/update/
  delete/reindex_fts) + `ItemFilter` dataclass
- [ ] `search/searcher.py` — `Searcher` Protocol + `SearchHit`,
  `SearchQuery`, `SearchMode` dataclasses/enums
- [ ] `embed/embedder.py` — `Embedder` Protocol + `ModelInfo` dataclass
- [ ] `embed/embedding_repo.py` — `EmbeddingRepo` Protocol
  (`upsert_status`, `get_status`, `list_failed`, `list_for_item`) +
  `EmbeddingStatus` dataclass. **Status-only — no vector methods.**
- [ ] `embed/model_registry.py` — `ModelRegistry` Protocol + `ModelSpec`
  dataclass
- [ ] `storage/filestore.py` — `FileStore` Protocol (defined here because
  storage/ owns the impl too; see Module Structure note above)
- [ ] `search/vectorstore.py` — `VectorStore` Protocol
  (`put`, `delete`, `search`) + `VectorQuery` dataclass + `VectorHit`
  dataclass. **Mirrors Go's `port/vectorstore.go` byte-for-byte —
  Go's actual fields are the contract:** `VectorQuery` has
  (vector, model, limit, scopes, kinds) — `scopes`/`kinds` are
  pushdown filters for the sqlite impl's JOIN on context_item;
  `VectorHit` has (id, score, distance) — RRF in `search/service.py`
  needs the raw `distance` alongside the normalized `score`. Earlier
  draft of this brief listed fewer fields; Go source governs per
  Global Constraints. **Owns the vec0 virtual table; this is where
  vector writes live.**
- [ ] `pdf/extractor.py` — `PDFExtractor` Protocol
- [ ] All as `@runtime_checkable Protocol` with method signatures
  matching Go's `port/*.go`. Drop `ctx context.Context` params — sync
  Python doesn't need them.
- [ ] Add module-specific error classes raised by these interfaces:
  - `embed/errors.py`: `ModelNotFound`, `ModelConflict` (UNIQUE
    violation on slug), `EmbeddingFailed`, `StatusNotFound` (raised
    by `EmbeddingRepo.get_status` when no row matches)
  - `pdf/errors.py`: `PDFEncrypted`, `PDFExtractionFailed`,
    `PDFCommandNotFound`
  - `storage/filestore.py` raises `items/errors.py:ExternalizedContentMissing`
- [ ] Tests: structural-typing smoke tests (a stub class satisfies each
  Protocol via `isinstance` check with `@runtime_checkable`).
- [ ] Commit: `feat: define Protocol interfaces + module errors`

### Task 1.6 — `tests/conftest.py` + `tests/_fakes/` — shared fixtures

Stubs are load-bearing across ~30 service-layer tests (Phase 5) and
~20 storage tests (Phase 2-3). Build them now or every Phase 2-6 test
re-invents them.

- [ ] `tests/_fakes/fake_repo.py` — `FakeContextRepo` in-memory impl of
  `items/repo.py:ContextRepo`. Port Go's `fake_repo_test.go` semantics
  (dict-backed, supports reindex_fts call recording).
- [ ] `tests/_fakes/canned_filestore.py` — `CannedFileStore` impl of
  `FileStore`. Constructor takes `dict[sha256, bytes]`; raises on
  unknown URI. Records `delete` calls for rollback verification.
- [ ] `tests/_fakes/fake_embedder.py` — `FakeEmbedder` impl of
  `embed/embedder.py:Embedder`. Configurable vectors + `error_embedder`
  variant for failure tests.
- [ ] `tests/conftest.py`:
  ```python
  import pytest
  from tests._fakes.fake_repo import FakeContextRepo
  from tests._fakes.canned_filestore import CannedFileStore
  from tests._fakes.fake_embedder import FakeEmbedder

  @pytest.fixture
  def fake_repo():
      return FakeContextRepo()

  @pytest.fixture
  def canned_fs():
      return CannedFileStore({})

  @pytest.fixture
  def fake_embedder():
      return FakeEmbedder(dimension=1024)

  @pytest.fixture
  def tmp_db(tmp_path):
      """Fresh :memory:-backed SQLite for storage tests."""
      from unictx.storage.db import open_db
      db = open_db(":memory:")
      yield db
      db.close()
  ```
- [ ] Tests: each fake has a smoke test verifying it satisfies its
  Protocol (`isinstance(fake, SomeProtocol)`).
- [ ] Commit: `feat(test): shared fixtures + fake stubs`

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
- [ ] `get(id)` — SELECT; **raise** `items/errors.py:ItemNotFound` if
  missing (not return, not bare `KeyError`).
- [ ] `update(item)` — UPDATE with version increment; AFTER UPDATE
  trigger rewrites FTS row
- [ ] `delete(id)` — DELETE; AFTER DELETE trigger removes FTS row
- [ ] `list(filter)` — paginated with cursor (base36 ts + ":" + id format)
- [ ] `reindex_fts(id, title, summary, content)` — direct INSERT into
  context_fts, bypassing the trigger pair
- [ ] `encode_cursor(ts, id)` / `decode_cursor(c)` — base36, byte-identical
  to Go's `strconv.FormatInt(ts, 36)`. Spike has the verified impl.
- [ ] `storage/row_factory.py:scan_item(cursor, row)` — JSON decode tags
  + source_meta, handle NULL. Registered globally as `db.row_factory`
  in `storage/db.py` so every SELECT returns `ContextItem` directly.
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

Port `internal/adapter/sqlite/vectorstore.go`. Owns the **vec0 virtual
table** (one per model slug + dimension: `vec_<slug>_<dim>`). This is
where vector writes live — distinct from EmbeddingRepo (Task 2.6,
status-only).

- [ ] `put(item_id, model_slug, vector)` — DELETE+INSERT in a single
  transaction (vec0 doesn't support INSERT OR REPLACE; this is the
  UPSERT idiom for virtual tables). Mirrors Go's `VectorStore.Put`.
- [ ] `delete(item_id, model_slug)` — used when an item is removed or
  a model is unregistered.
- [ ] `search(vector, model_slug, limit)` — JOIN vec0 virtual table
  with context_item, KNN match
- [ ] K=200 internal cap (matches Go)
- [ ] Same clamp_limit semantics as Searcher
- [ ] Tests: put/search roundtrip; put twice on same key replaces
  cleanly (no duplicate); delete removes; KNN finds embedded items;
  dimension mismatch handled; limit clamp
- [ ] Commit: `feat(storage): VectorStore impl using sqlite-vec`

### Task 2.6 — `storage/embedding_repo_impl.py`

Port `internal/adapter/sqlite/embedding_repo.go`. **EmbeddingRepo is
status-only** — it does NOT write vectors. Vector writes belong to
`VectorStore.Put` (Task 2.5). The two tables are separate:
`embedding_status` (regular table, UPSERT-able) vs `vec_<slug>_<dim>`
(vec0 virtual table, DELETE+INSERT in tx).

- [ ] `upsert_status(item_id, model_slug, status, err_str="")` — SQL
  matches Go's `upsertSQL` (embedding_repo.go:29-39) byte-for-byte:
  ```sql
  INSERT INTO context_embedding
      (item_id, model_slug, embedded_at, status, error, last_error, attempts)
  VALUES (?, ?, ?, ?, ?, ?, 1)
  ON CONFLICT(item_id, model_slug) DO UPDATE SET
      embedded_at = excluded.embedded_at,
      status      = excluded.status,
      error       = excluded.error,
      last_error  = excluded.last_error,
      attempts    = context_embedding.attempts + 1
  ```
  Notes: column is `embedded_at` (NOT `updated_at` — that doesn't exist
  on this table). Both `error` (0002 original) and `last_error` (0003
  addition) get bound to the same `err_str` for backward-compat — Go
  mirrors the same pattern. `embedded_at` value is
  `int(datetime.now(timezone.utc).timestamp())`.
- [ ] `get_status(item_id, model_slug)` — single-row read; raises
  `embed/errors.py:StatusNotFound` if missing.
- [ ] `list_failed(limit)` — `WHERE status='failed' ORDER BY embedded_at
  DESC LIMIT ?`. Used by Worker to retry. (Sort key is `embedded_at`,
  not `updated_at` — `context_embedding` has no `updated_at` column.)
- [ ] `list_for_item(item_id)` — all models' status rows for an item
  (drives `embed status` CLI command).
- [ ] Cascade: when a `context_item` is deleted, FK ON DELETE CASCADE
  (migration 0002/0004) removes the status row automatically — verify
  in test, don't write a manual cascade.
- [ ] Tests: upsert creates row; upsert same key again increments
  `attempts` and overwrites status/last_error; get_status happy + not
  found; list_failed ordering; list_for_item returns all models;
  cascade-on-item-delete.
- [ ] Commit: `feat(storage): EmbeddingRepo status-row impl (no vectors)`

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
- [ ] **Hybrid mode execution order (§3.7 clarification):**
  1. Call `embedder.Embed([query])` to get the query vector. This is
     the only HTTP call in the path — wrap with `concurrent.futures`
     (`ThreadPoolExecutor(max_workers=1)` + `future.result(timeout=N)`)
     OR rely on `httpx.Client(timeout=...)` set in the embedder itself
     (preferred — fewer moving parts). On timeout/error: fall back to
     fts-only with warning.
  2. Once query vector is in hand, both SQLite queries (FTS via
     `Searcher.search_fts` and KNN via `VectorStore.search`) are
     local + fast (<100ms typical). **No timeout wrapper needed**
     — they share a connection and run sequentially, not concurrently.
  3. RRF-merge the two result lists.
- [ ] **Per-leg timeout (§3.7):** applies to the embedder.Embed call
  in step 1 only. NOT to SQLite queries.
- [ ] **RRF formula (§3.8):** `Σ 1/(rank + 60)`, rank is post-filter
- [ ] **Over-fetch (§3.9):** 3× user limit on both legs; clamp to 200
- [ ] RRF tiebreak prefers newer items on score tie
- [ ] Tests: fts-only basic; hybrid basic; embedder timeout degrades
  to fts-only; embedder error degrades to fts-only; SQLite error in
  one leg returns the other leg's results with warning; ranking;
  tiebreak (newer wins on score tie)
- [ ] Commit: `feat(search): SearchService with RRF + embedder-only timeout`

### Task 5.3 — `embed/service.py` — EmbedService

Port `internal/service/embed.go`. EmbedService is reached via Worker /
Backfill / Reembed (NOT via IngestService — its skipEmbed short-circuits
before calling Embed, see §3.5 + Go ingest.go:273-280).

**Signature matches Go `Embed(ctx, itemID, title, content string) error`**
(embed.go:67) — NOT `EmbedItem(ctx, item, model_slug)`. Go passes
id+title+content strings, not a full ContextItem. `model_slug` is
**derived internally** from `embedder.Model().slug` (Go embed.go:69);
it's not a parameter. Hydration works by calling `repo.get(item_id)`
internally to fetch the full item (including `content_uri`).

- [ ] `embed_item(item_id, title, content)` flow:
  1. `model_slug = self.embedder.Model().slug` (derived, not parameter)
  2. If `content` empty, hydrate via `_hydrate_content(item_id)`:
     - `item = repo.get(item_id)` — fetches full ContextItem
     - If `item.content`: return it
     - If `item.content_uri`: `fs.get(item.content_uri)`
     - Else: return "" (title-only embed case)
  3. `text = (title + "\n\n" + content).strip()`. If empty: record
     `status='failed'` with err_str=`"embed: empty text for item <id>"`
     and **return the error** (Go embed.go:84-89). **NO 'skipped'
     status** — Go schema has only 'done' and 'failed'.
  4. Call `embedder.Embed([text])`.
  5. Split the write:
     - **Vector → `VectorStore.put(item_id, model_slug, vector)`** (vec0
       virtual table; Task 2.5)
     - **Status → `EmbeddingRepo.upsert_status(item_id, model_slug,
       'done', "")`** (regular table; Task 2.6)
  6. **Flip `any_embedding=1` on the context_item** (Go embed.go:107-120).
     Read item via `repo.get(id)`, set `item.any_embedding = 1`, call
     `repo.update(item)`. Flag-write failure is non-fatal: status stays
     'done' (vec row is the source of truth for "embedded"), warning
     logged.
- [ ] Status always written on every path (success → 'done', failure →
  'failed' + last_error + attempts++). Vector only written on success.
- [ ] Tests: happy path (vector + status='done' + any_embedding=1);
  empty content after hydration (status='failed', no vector, error
  raised); embedder failure (status='failed', no vector); VectorStore
  failure (status='failed', no flag flip); any_embedding flag-write
  failure (status stays 'done', warning logged, no error raised);
  model_slug comes from embedder not parameter
- [ ] Commit: `feat(embed): EmbedService with split vector/status writes`

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

### Task 6.1 — Typer app skeleton + global flags + wiring + output helper

- [ ] `cli/app.py` — main `typer.Typer()`
- [ ] Global flags: `--config`, `--json`, `--verbose`
- [ ] Config loading via `unictx.config.load(path)`
- [ ] App wiring: `wire(cfg)` returns a container with all services
  (the only place that imports `storage/*_impl.py` directly — everything
  else goes through service Protocols)
- [ ] `cli/output.py:print_json(result)` — single-purpose JSON helper,
  mirrors Go's `printJSON(v)` (output.go). NOT a branching `format_result`
  — non-JSON output is rendered per-command with rich tables.
  ```python
  import json
  from pathlib import Path
  from dataclasses import asdict

  def _default(obj):
      if isinstance(obj, Path):
          return str(obj)
      if hasattr(obj, "isoformat"):  # datetime
          return obj.isoformat()
      raise TypeError(f"not serializable: {type(obj)}")

  def print_json(result) -> None:
      """Print result as indented JSON to stdout.

      dataclasses.asdict handles ContextItem/Project/SearchHit;
      _default catches Path/datetime at the leaves. Domain models are
      @dataclass(slots=True) (not Pydantic), so Pydantic's serializer
      doesn't apply.
      """
      data = asdict(result) if hasattr(result, "__dataclass_fields__") else result
      typer.echo(json.dumps(data, default=_default, indent=2))
  ```
  Each subcommand does: `if json_mode: print_json(result); else:
  <rich table or plain print>`.
- [ ] Commit: `feat(cli): typer skeleton with global flags + wiring + output`

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
~50 test cases across all modules. Stubs from Task 1.6 are available.

- [ ] Service-layer tests using `FakeContextRepo` / `CannedFileStore` /
  `FakeEmbedder` from `tests/_fakes/`.
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
| sqlite-vec extension loading on sync sqlite3 | Already spike-validated (sync path verified). Async (aiosqlite) is fallback-only; not in critical path per sync-first binding |
| FTS5 malformed bug recurs in Python | Use fixed SQL from day 1 (Go archive §3.2); regression test in Task 2.4 |
| Cursor format incompatibility breaks pagination | Spike-validated byte-identical round-trip |
| PyMuPDF wheel availability on user's platform | PyMuPDF has broad wheel coverage; verify on first install |
| Pydantic v2 pulls in significant dep weight | Acceptable trade-off; Config-only scope keeps surface bounded; if dep weight becomes an issue, swap to plain dataclass + manual `__post_init__` validation (mechanical change) |
| Async creep inflates surface area | Sync-first mandate; only embedder HTTP calls async |
| Test porting underestimates | Phase 7 budgeted 5 full days (~50 cases); fakes built upfront in Task 1.6 |
| PDF perf regression (any engine slower than gxpdf) | PyMuPDF benchmark vs gxpdf in Task 4.1; abort + reconsider if >2× slower |
| User data loss during cutover | Mandatory backup before write-enable |
| Concurrent Go+Python writes during dev | Read-only Python during dev (URI `?mode=ro`) |
| Doctor `status: OK` lying about embedder failure (Go §5.8) | Fix in Python port — make status reflect reality |
| Module boundary erosion (cli reaching into storage) | ruff config + guard test in Task 1.1; reviewer enforces |

## Out of Scope

Explicitly NOT in this migration:

- **`ProjectRepo`.** Go has `port.ProjectRepo` + `sqlite/project_repo.go`,
  but they're **dead infrastructure** — wired in `app.go` (lines 32, 99)
  but no CLI command or service consumes them. `ContextItem.ProjectID`
  remains as a field (FK to `projects` table created by migration 0001),
  but Python does NOT port the repo or add a CLI for it. If project
  management is needed later, it's a separate plan.
- New features (agent framework, web UI, etc.) — those are separate
  plans once Python is primary.
- Schema redesign. The schema is identical (including the unused
  `projects` table, for byte-level migration compatibility).
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
