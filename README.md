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

## Status (2026-06-26)

- **Go implementation**: archived. Feature-complete through PDF Attach
  (commit `706de09` includes the malformed-FTS bugfix). No further
  development. Git history is preserved.
- **Python implementation**: planned. Migration plan + Go archive +
  spike results are committed; execution has not started.

See:
- `docs/superpowers/plans/2026-06-26-python-migration.md` — active plan
- `docs/superpowers/specs/2026-06-26-go-implementation-archive.md` — Go
  reference (architecture, invariants, bugs, gotchas)
- `python/spikes/migration-spike/spike.py` — pre-migration validation
  (6/6 risks passed)

## Why migrate?

See "Motivation" section of the migration plan. Summary:

1. **PDF library ecosystem** — Go's `gxpdf` was the only勉强-usable
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
