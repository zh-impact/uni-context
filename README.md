# uni-context

Personal context management — notes, PDFs, and search with hybrid
(FTS + vector) retrieval. Single user, single SQLite DB, no server.

## Repository Layout

This is a **monorepo** holding the historical Go implementation and the
in-progress Python port side-by-side. The Python port is the new primary;
Go is archived for reference.

```
uni-context/
├── archive/
│   └── go/                     # historical Go implementation (frozen)
│       ├── cmd/unictx/         # main package
│       ├── internal/           # hexagonal architecture (domain/port/adapter/service/cli/app/config)
│       ├── go.mod, go.sum
│       ├── Makefile
│       └── .github/workflows/  # Go-specific CI (frozen)
├── python/                     # new primary implementation
│   ├── pyproject.toml          # (to be created in Phase 1)
│   ├── src/unictx/             # modular monolith: items/ search/ embed/ pdf/ storage/ cli/
│   ├── tests/
│   └── spikes/
│       └── migration-spike/    # pre-migration validation (passed)
├── docs/                       # cross-project documentation
│   └── superpowers/
│       ├── plans/2026-06-26-python-migration.md   # active migration plan
│       └── specs/2026-06-26-go-implementation-archive.md  # Go reference
└── CHANGELOG.md                # cross-project change log
```

## Status (2026-06-28)

- **Go implementation**: archived. Feature-complete through PDF Attach
  (commit `706de09` includes the malformed-FTS bugfix). No further
  development. Git history is preserved.
- **Python implementation**: **primary, feature-complete through Phase 7**
  of the migration plan. Phases 1-7 shipped: domain types, storage layer
  (FTS5 + sqlite-vec + migrations 0001-0004), embedders (Ollama +
  OpenAI-compat), PDF engines (PyMuPDF/shell/http), services (Ingest,
  Search, Embed, Worker, Backfill, Reembed, ReindexFTS, ItemService,
  ModelService, DiagnosticService), full CLI (Typer with `user note
  add|get|list|delete`, `search`, `embed model|switch|backfill|worker|
  reembed|status`, `doctor`, `reindex-fts`). **577 tests passing.**
- **Phase 8 (cutover)**: Task 8.1 (read-only parity verification) ran
  against a real Go-format DB at `~/dotfiles/local/share/unictx/unictx.db`
  (XDG_DATA_HOME override) — 19 user notes + 17 embeddings under the
  OpenAI default model. All read paths verified: `doctor` (schema v4 +
  embedder ping), `user note list/get` (inline + externalized PDF
  hydration), `search` (FTS, hybrid with real RRF over the embedded
  corpus), `embed model list`, `embed status` (with and without
  embeddings), `reindex-fts --dry-run`. The verification surfaced one
  formatter bug (`embed status` crashed on int-typed `embedded_at`),
  fixed in `a4d2f32`. Task 8.2 (backup + read-write cutover) remains
  pending explicit user authorization — destructive ops on the source
  DB are not in scope for read-only parity.

See:
- `docs/superpowers/plans/2026-06-26-python-migration.md` — the plan
  (Phases 1-7 complete; Phase 8 partially complete)
- `docs/superpowers/specs/2026-06-26-go-implementation-archive.md` — Go
  reference (architecture, invariants, bugs, gotchas)
- `python/spikes/migration-spike/spike.py` — pre-migration validation
  (6/6 risks passed)
- `.superpowers/sdd/progress.md` — per-task progress ledger (commits,
  test counts, design notes)

## Why migrate?

See "Motivation" section of the migration plan. Summary:

1. **PDF library ecosystem** — Go's `gxpdf` was the only Barely-usable
   option; Python has PyMuPDF (fast, AES-256-capable) plus many others.
2. **Future web/agent layer** — Python ecosystem dominates agent frameworks.
3. **Iterate faster** — REPL + hot reload > Go compile cycle.
4. **Author familiarity** — Python is the author's stronger language.

## Architecture Decision

Python implementation uses **modular monolith**, not DDD/hexagonal.
The Go implementation's hexagonal layering (domain/port/adapter/service)
was over-engineered for a single-user SQLite-bound tool. The Python port
collapses layers into feature-organized modules (`items/`, `search/`,
`embed/`, etc.) with direct cross-module imports.

**Invariants from the Go implementation are preserved** (PDF branch
ordering, rollback contract, embed-skip scope, RRF formula, cursor
format, malformed-FTS SQL fix). See Go archive §3 for the full list.

Structure can change; invariants shouldn't.
