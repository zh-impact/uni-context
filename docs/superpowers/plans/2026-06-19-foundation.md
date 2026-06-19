# uni-context Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundation of uni-context — Go module skeleton, domain model, SQLite-backed storage with FTS5 search, ingest service, and a minimal CLI that lets the user add notes and search them with full-text search only.

**Architecture:** Hexagonal / ports-and-adapters. Pure domain package at center, services orchestrate use cases via port interfaces, adapters (sqlite, fsstore) implement those ports. CLI is the only interface exposed in this plan; HTTP and MCP come in later plans. SQLite via cgo (`mattn/go-sqlite3`) with FTS5 (trigram tokenizer) for keyword search. Embedding/vector search deferred to Plan 2.

**Tech Stack:**
- Go 1.22+ (for `slog`, `maps`, etc.)
- `github.com/mattn/go-sqlite3` (cgo SQLite driver)
- `github.com/spf13/cobra` v1.8+ (CLI)
- `github.com/google/uuid` v1.6+ (UUIDv7 for sortable IDs)
- `gopkg.in/yaml.v3` (config)
- `github.com/stretchr/testify` (test assertions)
- SQLite FTS5 with `trigram` tokenizer

## Global Constraints

Copied verbatim from `docs/superpowers/specs/2026-06-19-uni-context-design.md`:

- Module name: `uni-context`
- SQLite driver: `github.com/mattn/go-sqlite3` (cgo, NOT modernc)
- SQLite PRAGMAs (set at connection open): `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`, `temp_store=MEMORY`
- FTS5 tokenizer: `trigram` (NOT `unicode61` — better CJK recall)
- Content externalization threshold: 4KB inline, larger goes to filestore
- ID format: UUID v7 (time-sortable)
- CLI exit codes: `0` ok / `1` general error / `2` arg error / `3` DB error / `4` external dep unavailable
- stdout = data, stderr = logs/errors
- Data dir: `~/.local/share/unictx/` (XDG); config dir: `~/.config/unictx/`
- TDD discipline: failing test → minimal impl → green → commit. Every task.
- Race detector on for all tests: `go test -race ./...`

---

## File Structure

Files created or modified by this plan:

```
uni-context/
├── go.mod                                    # Task 1
├── go.sum                                    # Task 1
├── Makefile                                  # Task 1
├── .gitignore                                # Task 1
├── cmd/unictx/main.go                        # Task 1, 11
├── internal/
│   ├── domain/
│   │   ├── context.go                        # Task 2 — ContextItem, Scope, Kind, Source
│   │   ├── context_test.go                   # Task 2
│   │   ├── errors.go                         # Task 2
│   │   ├── project.go                        # Task 2
│   │   └── project_test.go                   # Task 2
│   ├── port/
│   │   ├── repository.go                     # Task 3 — ContextRepo interface
│   │   ├── filestore.go                      # Task 3 — FileStore interface
│   │   └── searcher.go                       # Task 3 — Searcher interface
│   ├── adapter/
│   │   ├── sqlite/
│   │   │   ├── db.go                         # Task 4 — Open, PRAGMAs
│   │   │   ├── migrations.go                 # Task 4 — embed + run migrations
│   │   │   ├── migrations/
│   │   │   │   └── 0001_init.sql             # Task 4 — initial schema
│   │   │   ├── migrations_test.go            # Task 4
│   │   │   ├── repo.go                       # Task 5 — ContextRepo impl
│   │   │   ├── repo_test.go                  # Task 5
│   │   │   ├── project_repo.go               # Task 5
│   │   │   ├── project_repo_test.go          # Task 5
│   │   │   ├── searcher.go                   # Task 6 — FTS Searcher impl
│   │   │   └── searcher_test.go              # Task 6
│   │   └── fsstore/
│   │       ├── store.go                      # Task 7
│   │       └── store_test.go                 # Task 7
│   ├── service/
│   │   ├── ingest.go                         # Task 8
│   │   ├── ingest_test.go                    # Task 8
│   │   ├── search.go                         # Task 9
│   │   └── search_test.go                    # Task 9
│   ├── app/
│   │   └── app.go                            # Task 10 — wireApp
│   ├── config/
│   │   ├── config.go                         # Task 10
│   │   └── config_test.go                    # Task 10
│   └── cli/
│       ├── root.go                           # Task 11
│       ├── config_cmd.go                     # Task 11
│       ├── doctor.go                         # Task 11
│       ├── user_note.go                      # Task 12
│       ├── search.go                         # Task 13
│       └── output.go                         # Task 12 — JSON helpers
├── .github/workflows/
│   └── test.yml                              # Task 14
└── internal/cli/e2e_test.go                  # Task 14
```

---

## Task 1: Project Scaffolding

**Files:**
- Create: `go.mod`, `Makefile`, `.gitignore`, `cmd/unictx/main.go`

**Interfaces:**
- Produces: Go module `uni-context`, executable stub `unictx` that prints version

- [ ] **Step 1: Initialize Go module**

```bash
cd /Users/gege/sourcecode/4MVP/uni-context
go mod init uni-context
```

- [ ] **Step 2: Add module dependencies**

```bash
go get github.com/mattn/go-sqlite3@latest
go get github.com/spf13/cobra@v1.8.1
go get github.com/google/uuid@v1.6.0
go get gopkg.in/yaml.v3@latest
go get github.com/stretchr/testify@latest
```

- [ ] **Step 3: Create main.go stub**

Create `cmd/unictx/main.go`:

```go
package main

import (
    "fmt"
    "os"
)

var version = "dev"

func main() {
    if len(os.Args) > 1 && os.Args[1] == "--version" {
        fmt.Println(version)
        return
    }
    fmt.Fprintln(os.Stderr, "uni-context", version, "(skeleton — see Plan 1 task 11+)")
    os.Exit(1)
}
```

- [ ] **Step 4: Create .gitignore**

Create `.gitignore`:

```
# Go
/dist/
*.test
*.out
coverage.txt

# Build artifacts
/unictx
/bin/

# IDE
.vscode/
.idea/
*.swp

# macOS
.DS_Store

# Local data (never commit user data)
/.local/
```

- [ ] **Step 5: Create Makefile**

Create `Makefile`:

```makefile
VERSION ?= dev
BIN     ?= unictx
PKG     := ./...

.PHONY: build test test-race lint fmt vet clean install

build:
	CGO_ENABLED=1 go build -ldflags "-X main.version=$(VERSION)" -o $(BIN) ./cmd/unictx

test:
	CGO_ENABLED=1 go test $(PKG)

test-race:
	CGO_ENABLED=1 go test -race $(PKG)

test-integration:
	CGO_ENABLED=1 go test -tags=integration $(PKG)

fmt:
	gofmt -s -w .

vet:
	go vet $(PKG)

lint: vet
	@command -v golangci-lint >/dev/null 2>&1 && golangci-lint run || echo "golangci-lint not installed; skipping"

clean:
	rm -f $(BIN) coverage.txt
	go clean -testcache

install: build
	mv $(BIN) $(GOPATH)/bin/$(BIN)
```

- [ ] **Step 6: Verify build works**

```bash
CGO_ENABLED=1 go build -o /tmp/unictx ./cmd/unictx
/tmp/unictx --version
```

Expected output: `dev`

- [ ] **Step 7: Commit**

```bash
git add go.mod go.sum Makefile .gitignore cmd/
git commit -m "chore: scaffold uni-context Go module"
```

---

## Task 2: Domain Core (ContextItem, Scope, Kind, Source)

**Files:**
- Create: `internal/domain/context.go`, `internal/domain/context_test.go`, `internal/domain/errors.go`, `internal/domain/project.go`, `internal/domain/project_test.go`

**Interfaces:**
- Produces: `domain.ContextItem` struct, `domain.Scope` / `domain.Kind` / `domain.Source` / `domain.Visibility` types and their valid values, `domain.ErrValidation` / `domain.ErrNotFound` error sentinels, `domain.Project` struct, `domain.NewContextItem()` constructor that enforces invariants.

- [ ] **Step 1: Write the failing test for Scope/Kind/Source validation**

Create `internal/domain/context_test.go`:

```go
package domain

import (
    "strings"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
)

func TestNewContextItem_ValidCombinations(t *testing.T) {
    cases := []struct {
        name   string
        scope  Scope
        kind   Kind
        source Source
        owner  string
        project string
    }{
        {"user note manual", ScopeUser, KindNote, SourceManual, "user-1", ""},
        {"project doc import", ScopeProject, KindDoc, SourceImport, "user-1", "proj-1"},
        {"global link import", ScopeGlobal, KindLink, SourceImport, "", ""},
        {"project conversation agent", ScopeProject, KindConversationMsg, SourceAgent, "", "proj-1"},
        {"project memory sync", ScopeProject, KindMemory, SourceSync, "", "proj-1"},
    }
    for _, tc := range cases {
        t.Run(tc.name, func(t *testing.T) {
            item, err := NewContextItem(tc.scope, tc.kind, tc.source, NewItemParams{
                OwnerUserID: tc.owner, ProjectID: tc.project,
            })
            require.NoError(t, err)
            assert.NotEmpty(t, item.ID)
            assert.Equal(t, tc.scope, item.Scope)
        })
    }
}

func TestNewContextItem_InvalidCombinations(t *testing.T) {
    cases := []struct {
        name   string
        scope  Scope
        kind   Kind
        source Source
        owner  string
        project string
        wantErrSub string
    }{
        {"global with owner", ScopeGlobal, KindNote, SourceManual, "u", "", "global must not have owner"},
        {"global with project", ScopeGlobal, KindNote, SourceManual, "", "p", "global must not have project"},
        {"user without owner", ScopeUser, KindNote, SourceManual, "", "", "user scope requires owner"},
        {"project without project", ScopeProject, KindDoc, SourceManual, "u", "", "project scope requires project_id"},
        {"memory without agent source", ScopeProject, KindMemory, SourceManual, "u", "p", "memory kind requires source=agent or sync"},
        {"memory with wrong scope", ScopeUser, KindMemory, SourceAgent, "u", "", "memory kind requires project scope"},
    }
    for _, tc := range cases {
        t.Run(tc.name, func(t *testing.T) {
            _, err := NewContextItem(tc.scope, tc.kind, tc.source, NewItemParams{
                OwnerUserID: tc.owner, ProjectID: tc.project,
            })
            require.Error(t, err)
            assert.True(t, strings.Contains(err.Error(), tc.wantErrSub),
                "err=%q want substring %q", err.Error(), tc.wantErrSub)
        })
    }
}

func TestNewContextItem_AssignsUUIDv7(t *testing.T) {
    item1, _ := NewContextItem(ScopeUser, KindNote, SourceManual, NewItemParams{OwnerUserID: "u"})
    item2, _ := NewContextItem(ScopeUser, KindNote, SourceManual, NewItemParams{OwnerUserID: "u"})
    assert.NotEqual(t, item1.ID, item2.ID)
    // UUIDv7 has 48-bit timestamp prefix; first char should match for nearby IDs
    assert.Equal(t, item1.ID[:1], item2.ID[:1])
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
CGO_ENABLED=1 go test ./internal/domain/...
```

Expected: build failure — `domain.NewContextItem` undefined.

- [ ] **Step 3: Write errors.go**

Create `internal/domain/errors.go`:

```go
package domain

import "errors"

var (
    ErrNotFound   = errors.New("not found")
    ErrValidation = errors.New("validation")
    ErrConflict   = errors.New("conflict")
)
```

- [ ] **Step 4: Write context.go**

Create `internal/domain/context.go`:

```go
package domain

import (
    "fmt"
    "time"

    "github.com/google/uuid"
)

// Scope of a ContextItem.
type Scope string

const (
    ScopeUser    Scope = "user"
    ScopeProject Scope = "project"
    ScopeGlobal  Scope = "global"
)

// Kind of content.
type Kind string

const (
    KindNote            Kind = "note"
    KindExcerpt         Kind = "excerpt"
    KindLink            Kind = "link"
    KindDoc             Kind = "doc"
    KindConversationMsg Kind = "conversation_msg"
    KindMemory          Kind = "memory"
    KindFile            Kind = "file"
)

// Source of a ContextItem.
type Source string

const (
    SourceManual  Source = "manual"
    SourceAgent   Source = "agent"
    SourceSync    Source = "sync"
    SourceImport  Source = "import"
    SourceWebhook Source = "webhook"
)

// Visibility of a ContextItem.
type Visibility string

const (
    VisibilityPrivate Visibility = "private"
    VisibilityProject Visibility = "project"
    VisibilityPublic  Visibility = "public"
)

// ContextItem is the unified entity for all knowledge in the system.
type ContextItem struct {
    ID             string
    Scope          Scope
    Kind           Kind
    Source         Source
    OwnerUserID    string
    ProjectID      string
    AgentID        string
    ConversationID string
    ParentID       string

    Title    string
    Summary  string
    Content  string // inline, <= 4KB
    ContentURI string
    ContentMIME string
    ContentHash string
    Language string

    Tags       []string
    SourceMeta map[string]any
    Visibility Visibility
    Confidence float64

    WordCount   int
    AnyEmbedding int // 0 or 1; always 0 in Plan 1
    CreatedAt   time.Time
    UpdatedAt   time.Time
    Version     int
}

// NewItemParams holds optional fields for NewContextItem.
type NewItemParams struct {
    OwnerUserID string
    ProjectID   string
    AgentID     string
}

// ContentInlineLimit is the max byte length stored inline in Content.
const ContentInlineLimit = 4 * 1024

// NewContextItem constructs an item with scope/kind/source invariants enforced.
func NewContextItem(scope Scope, kind Kind, source Source, params NewItemParams) (ContextItem, error) {
    if err := validateCombination(scope, kind, source, params); err != nil {
        return ContextItem{}, fmt.Errorf("%w: %s", ErrValidation, err.Error())
    }
    now := time.Now().UTC()
    id, err := uuid.NewV7()
    if err != nil {
        return ContextItem{}, fmt.Errorf("generate id: %w", err)
    }
    return ContextItem{
        ID:          id.String(),
        Scope:       scope,
        Kind:        kind,
        Source:      source,
        OwnerUserID: params.OwnerUserID,
        ProjectID:   params.ProjectID,
        AgentID:     params.AgentID,
        Visibility:  VisibilityPrivate,
        Confidence:  1.0,
        Tags:        []string{},
        SourceMeta:  map[string]any{},
        CreatedAt:   now,
        UpdatedAt:   now,
        Version:     1,
    }, nil
}

func validateCombination(scope Scope, kind Kind, source Source, p NewItemParams) error {
    switch scope {
    case ScopeGlobal:
        if p.OwnerUserID != "" {
            return fmt.Errorf("global scope must not have owner")
        }
        if p.ProjectID != "" {
            return fmt.Errorf("global scope must not have project")
        }
    case ScopeUser:
        if p.OwnerUserID == "" {
            return fmt.Errorf("user scope requires owner")
        }
        if p.ProjectID != "" {
            return fmt.Errorf("user scope must not have project (use project scope)")
        }
    case ScopeProject:
        if p.ProjectID == "" {
            return fmt.Errorf("project scope requires project_id")
        }
    default:
        return fmt.Errorf("unknown scope %q", scope)
    }

    if kind == KindMemory {
        if scope != ScopeProject {
            return fmt.Errorf("memory kind requires project scope")
        }
        if source != SourceAgent && source != SourceSync {
            return fmt.Errorf("memory kind requires source=agent or sync")
        }
    }
    if kind == KindConversationMsg {
        if scope != ScopeProject {
            return fmt.Errorf("conversation_msg kind requires project scope")
        }
        if source != SourceAgent && source != SourceSync {
            return fmt.Errorf("conversation_msg kind requires source=agent or sync")
        }
    }
    return nil
}
```

- [ ] **Step 5: Run test to verify it passes**

```bash
CGO_ENABLED=1 go test ./internal/domain/...
```

Expected: PASS.

- [ ] **Step 6: Write project_test.go**

Create `internal/domain/project_test.go`:

```go
package domain

import (
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
)

func TestNewProject_AssignsIDAndTimestamps(t *testing.T) {
    p, err := NewProject("my-app", "/path/to/app", "desc")
    require.NoError(t, err)
    assert.NotEmpty(t, p.ID)
    assert.Equal(t, "my-app", p.Name)
    assert.False(t, p.CreatedAt.IsZero())
}

func TestNewProject_RejectsEmptyName(t *testing.T) {
    _, err := NewProject("", "/path", "")
    require.Error(t, err)
}
```

- [ ] **Step 7: Write project.go**

Create `internal/domain/project.go`:

```go
package domain

import (
    "fmt"
    "time"

    "github.com/google/uuid"
)

type Project struct {
    ID          string
    Name        string
    Path        string
    Description string
    CreatedAt   time.Time
    UpdatedAt   time.Time
}

func NewProject(name, path, description string) (Project, error) {
    if name == "" {
        return Project{}, fmt.Errorf("%w: project name required", ErrValidation)
    }
    id, err := uuid.NewV7()
    if err != nil {
        return Project{}, fmt.Errorf("generate id: %w", err)
    }
    now := time.Now().UTC()
    return Project{
        ID: id.String(), Name: name, Path: path, Description: description,
        CreatedAt: now, UpdatedAt: now,
    }, nil
}
```

- [ ] **Step 8: Run all domain tests**

```bash
CGO_ENABLED=1 go test -race ./internal/domain/...
```

Expected: PASS, no race warnings.

- [ ] **Step 9: Commit**

```bash
git add internal/domain/
git commit -m "feat(domain): add ContextItem, Project, and validation invariants"
```

---

## Task 3: Port Interfaces

**Files:**
- Create: `internal/port/repository.go`, `internal/port/filestore.go`, `internal/port/searcher.go`

**Interfaces:**
- Consumes: `domain.ContextItem`, `domain.Project`
- Produces: `port.ContextRepo`, `port.ProjectRepo`, `port.FileStore`, `port.Searcher` interfaces; `port.SearchQuery` / `port.SearchResult` / `port.ItemFilter` types.

- [ ] **Step 1: Write repository.go**

Create `internal/port/repository.go`:

```go
package port

import (
    "context"

    "uni-context/internal/domain"
)

// ItemFilter narrows a list/search query.
type ItemFilter struct {
    Scopes   []domain.Scope
    Kinds    []domain.Kind
    Tags     []string // AND semantics
    OwnerUserID string
    ProjectID   string
    Cursor   string // opaque; created_at + id encoded
    Limit    int
}

// ContextRepo is the persistence port for ContextItem.
type ContextRepo interface {
    Create(ctx context.Context, item domain.ContextItem) error
    Get(ctx context.Context, id string) (domain.ContextItem, error)
    Update(ctx context.Context, item domain.ContextItem) error
    Delete(ctx context.Context, id string) error
    List(ctx context.Context, filter ItemFilter) ([]domain.ContextItem, string, error)
    // NextCursor builds an opaque cursor from the last item returned.
    NextCursor(item domain.ContextItem) string
}

// ProjectRepo is the persistence port for Project.
type ProjectRepo interface {
    Create(ctx context.Context, p domain.Project) error
    GetByName(ctx context.Context, name string) (domain.Project, error)
    List(ctx context.Context) ([]domain.Project, error)
    Delete(ctx context.Context, id string) error
}
```

- [ ] **Step 2: Write filestore.go**

Create `internal/port/filestore.go`:

```go
package port

// FileStore holds large content blobs (>4KB) on disk, addressed by sha256 hash.
type FileStore interface {
    // Put writes content and returns a content_uri ("file://<relative-path>")
    // and the sha256 hash. If content already exists, returns existing URI.
    Put(content []byte, mime string) (uri string, hash string, err error)
    // Get retrieves content by uri.
    Get(uri string) ([]byte, error)
    // Delete decrements refcount; file removed only when refcount hits 0.
    Delete(uri string) error
}
```

- [ ] **Step 3: Write searcher.go**

Create `internal/port/searcher.go`:

```go
package port

import "context"

// SearchQuery defines a full-text search.
type SearchQuery struct {
    Query   string
    Limit   int
    // Future: filter by scope/kind/tags via FTS WHERE — added in service.Search wrapper
}

// SearchHit is one BM25 search result.
type SearchHit struct {
    ID       string
    Score    float64
    Snippet  string
}

// Searcher does keyword search (BM25 via FTS5 in this plan).
type Searcher interface {
    SearchFTS(ctx context.Context, q SearchQuery) ([]SearchHit, error)
}
```

- [ ] **Step 4: Verify it compiles**

```bash
go build ./internal/port/...
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add internal/port/
git commit -m "feat(port): define ContextRepo, ProjectRepo, FileStore, Searcher interfaces"
```

---

## Task 4: SQLite Migrations Framework

**Files:**
- Create: `internal/adapter/sqlite/db.go`, `internal/adapter/sqlite/migrations.go`, `internal/adapter/sqlite/migrations_test.go`, `internal/adapter/sqlite/migrations/0001_init.sql`

**Interfaces:**
- Consumes: nothing (foundational)
- Produces: `sqlite.Open(dbPath) (*sql.DB, error)` — opens DB with PRAGMAs and runs pending migrations.

- [ ] **Step 1: Write the initial schema migration**

Create `internal/adapter/sqlite/migrations/0001_init.sql`:

```sql
-- Schema metadata
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '0');

-- Project (basic, for forward-compat with later plans)
CREATE TABLE IF NOT EXISTS project (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    path        TEXT,
    description TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

-- Core item table (full schema per spec §3.1)
CREATE TABLE IF NOT EXISTS context_item (
    id              TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    source          TEXT NOT NULL,
    owner_user_id   TEXT,
    project_id      TEXT REFERENCES project(id) ON DELETE SET NULL,
    agent_id        TEXT,
    conversation_id TEXT,
    parent_id       TEXT,
    title           TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    content         TEXT NOT NULL DEFAULT '',
    content_uri     TEXT,
    content_mime    TEXT,
    content_hash    TEXT,
    language        TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',
    source_meta     TEXT NOT NULL DEFAULT '{}',
    visibility      TEXT NOT NULL DEFAULT 'private',
    confidence      REAL NOT NULL DEFAULT 1.0,
    word_count      INTEGER NOT NULL DEFAULT 0,
    any_embedding   INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_item_scope_created ON context_item(scope, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_item_project       ON context_item(project_id) WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_item_kind          ON context_item(kind);
CREATE INDEX IF NOT EXISTS idx_item_owner         ON context_item(owner_user_id) WHERE owner_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_item_hash          ON context_item(content_hash) WHERE content_hash IS NOT NULL;

-- FTS5 (trigram tokenizer for CJK friendliness)
CREATE VIRTUAL TABLE IF NOT EXISTS context_fts USING fts5(
    title, summary, content,
    content='context_item', content_rowid='rowid',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS context_ai AFTER INSERT ON context_item BEGIN
    INSERT INTO context_fts(rowid, title, summary, content)
    VALUES (new.rowid, new.title, new.summary, new.content);
END;

CREATE TRIGGER IF NOT EXISTS context_ad AFTER DELETE ON context_item BEGIN
    INSERT INTO context_fts(context_fts, rowid, title, summary, content)
    VALUES ('delete', old.rowid, old.title, old.summary, old.content);
END;

CREATE TRIGGER IF NOT EXISTS context_au AFTER UPDATE ON context_item BEGIN
    INSERT INTO context_fts(context_fts, rowid, title, summary, content)
    VALUES ('delete', old.rowid, old.title, old.summary, old.content);
    INSERT INTO context_fts(rowid, title, summary, content)
    VALUES (new.rowid, new.title, new.summary, new.content);
END;

UPDATE schema_meta SET value = '1' WHERE key = 'schema_version';
```

- [ ] **Step 2: Write the failing test for migrations**

Create `internal/adapter/sqlite/migrations_test.go`:

```go
package sqlite

import (
    "database/sql"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
    _ "github.com/mattn/go-sqlite3"
)

func TestMigrations_RunOnFreshDB(t *testing.T) {
    db, err := sql.Open("sqlite3", ":memory:")
    require.NoError(t, err)
    defer db.Close()

    require.NoError(t, Migrate(db))

    var version string
    err = db.QueryRow(`SELECT value FROM schema_meta WHERE key='schema_version'`).Scan(&version)
    require.NoError(t, err)
    assert.Equal(t, "1", version)

    // Tables exist
    for _, table := range []string{"context_item", "context_fts", "project", "schema_meta"} {
       	var name string
        err = db.QueryRow(
            `SELECT name FROM sqlite_master WHERE type='table' AND name=?`, table,
        ).Scan(&name)
        require.NoError(t, err, "table %s should exist", table)
    }
}

func TestMigrations_Idempotent(t *testing.T) {
    db, _ := sql.Open("sqlite3", ":memory:")
    defer db.Close()

    require.NoError(t, Migrate(db))
    require.NoError(t, Migrate(db)) // second run is a no-op
}
```

- [ ] **Step 3: Run test to verify it fails**

```bash
CGO_ENABLED=1 go test ./internal/adapter/sqlite/...
```

Expected: FAIL — `sqlite.Migrate` undefined.

- [ ] **Step 4: Write migrations.go**

Create `internal/adapter/sqlite/migrations.go`:

```go
package sqlite

import (
    "context"
    "database/sql"
    "embed"
    "fmt"
    "io/fs"
    "regexp"
    "sort"
    "strconv"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

var versionRE = regexp.MustCompile(`(\d+)_.*\.sql$`)

// Migrate runs all pending migrations in order.
func Migrate(db *sql.DB) error {
    if err := ensureSchemaMeta(db); err != nil {
        return err
    }
    current, err := readVersion(db)
    if err != nil {
        return err
    }

    files, err := sortedMigrationFiles()
    if err != nil {
        return err
    }

    for _, fname := range files {
        v := versionFromName(fname)
        if v <= current {
            continue
        }
        content, err := migrationFS.ReadFile("migrations/" + fname)
        if err != nil {
            return fmt.Errorf("read migration %s: %w", fname, err)
        }
        if err := execMigration(db, fname, string(content)); err != nil {
            return err
        }
    }
    return nil
}

func ensureSchemaMeta(db *sql.DB) error {
    _, err := db.Exec(`CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )`)
    if err != nil {
        return err
    }
    _, err = db.Exec(`INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '0')`)
    return err
}

func readVersion(db *sql.DB) (int, error) {
    var s string
    err := db.QueryRow(`SELECT value FROM schema_meta WHERE key='schema_version'`).Scan(&s)
    if err != nil {
        return 0, err
    }
    return strconv.Atoi(s)
}

func sortedMigrationFiles() ([]string, error) {
    entries, err := fs.ReadDir(migrationFS, "migrations")
    if err != nil {
        return nil, err
    }
    var names []string
    for _, e := range entries {
        if !e.IsDir() {
            names = append(names, e.Name())
        }
    }
    sort.Strings(names)
    return names, nil
}

func versionFromName(name string) int {
    m := versionRE.FindStringSubmatch(name)
    if len(m) < 2 {
        return 0
    }
    v, _ := strconv.Atoi(m[1])
    return v
}

// execMigration wraps the entire migration body in a single transaction
// (SQLite handles DDL transactionally). It does NOT parse statements —
// migrations are authored to be executable as one Exec call.
func execMigration(db *sql.DB, fname, body string) error {
    tx, err := db.BeginTx(context.Background(), nil)
    if err != nil {
        return fmt.Errorf("begin tx for %s: %w", fname, err)
    }
    if _, err := tx.Exec(body); err != nil {
        _ = tx.Rollback()
        return fmt.Errorf("exec migration %s: %w", fname, err)
    }
    return tx.Commit()
}
```

- [ ] **Step 5: Write db.go**

Create `internal/adapter/sqlite/db.go`:

```go
package sqlite

import (
    "database/sql"
    "fmt"

    _ "github.com/mattn/go-sqlite3"
)

// Open opens a SQLite database at dbPath (file path or ":memory:") with the
// PRAGMAs specified in the global constraints, then runs migrations.
func Open(dbPath string) (*sql.DB, error) {
    dsn := fmt.Sprintf("file:%s?_journal_mode=WAL&_synchronous=NORMAL&_busy_timeout=5000&_foreign_keys=on&_temp_store=MEMORY", dbPath)
    db, err := sql.Open("sqlite3", dsn)
    if err != nil {
        return nil, fmt.Errorf("open sqlite: %w", err)
    }
    // SQLite doesn't error on open until first use; ping to surface issues.
    if err := db.Ping(); err != nil {
        _ = db.Close()
        return nil, fmt.Errorf("ping sqlite: %w", err)
    }
    if err := Migrate(db); err != nil {
        _ = db.Close()
        return nil, fmt.Errorf("migrate: %w", err)
    }
    return db, nil
}
```

- [ ] **Step 6: Run test to verify it passes**

```bash
CGO_ENABLED=1 go test -race ./internal/adapter/sqlite/...
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add internal/adapter/sqlite/
git commit -m "feat(adapter/sqlite): add migration framework and initial schema"
```

---

## Task 5: SQLite ContextRepo + ProjectRepo

**Files:**
- Create: `internal/adapter/sqlite/repo.go`, `internal/adapter/sqlite/repo_test.go`, `internal/adapter/sqlite/project_repo.go`, `internal/adapter/sqlite/project_repo_test.go`

**Interfaces:**
- Consumes: `port.ContextRepo` / `port.ProjectRepo` / `port.ItemFilter` / `domain.*`
- Produces: `sqlite.NewContextRepo(db)` and `sqlite.NewProjectRepo(db)` constructors.

- [ ] **Step 1: Write the failing test for ContextRepo CRUD**

Create `internal/adapter/sqlite/repo_test.go`:

```go
package sqlite

import (
    "context"
    "database/sql"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
    "uni-context/internal/domain"
    "uni-context/internal/port"
)

func setupRepo(t *testing.T) (port.ContextRepo, *sql.DB) {
    t.Helper()
    db, err := sql.Open("sqlite3", ":memory:")
    require.NoError(t, err)
    require.NoError(t, Migrate(db))
    t.Cleanup(func() { db.Close() })
    return NewContextRepo(db), db
}

func newItem(t *testing.T, scope domain.Scope, kind domain.Kind, source domain.Source) domain.ContextItem {
    t.Helper()
    params := domain.NewItemParams{OwnerUserID: "u-1"}
    if scope == domain.ScopeProject {
        params = domain.NewItemParams{OwnerUserID: "u-1", ProjectID: "p-1"}
    }
    if scope == domain.ScopeGlobal {
        params = domain.NewItemParams{}
    }
    item, err := domain.NewContextItem(scope, kind, source, params)
    require.NoError(t, err)
    item.Title = "Test Note"
    item.Content = "Hello world from a test note."
    return item
}

func TestContextRepo_CreateAndGet(t *testing.T) {
    repo, _ := setupRepo(t)
    ctx := context.Background()
    item := newItem(t, domain.ScopeUser, domain.KindNote, domain.SourceManual)

    require.NoError(t, repo.Create(ctx, item))

    got, err := repo.Get(ctx, item.ID)
    require.NoError(t, err)
    assert.Equal(t, item.ID, got.ID)
    assert.Equal(t, "Test Note", got.Title)
    assert.Equal(t, "Hello world from a test note.", got.Content)
    assert.Equal(t, []string{}, got.Tags)
}

func TestContextRepo_GetNotFound(t *testing.T) {
    repo, _ := setupRepo(t)
    _, err := repo.Get(context.Background(), "nonexistent")
    assert.ErrorIs(t, err, domain.ErrNotFound)
}

func TestContextRepo_Delete(t *testing.T) {
    repo, _ := setupRepo(t)
    ctx := context.Background()
    item := newItem(t, domain.ScopeUser, domain.KindNote, domain.SourceManual)
    require.NoError(t, repo.Create(ctx, item))

    require.NoError(t, repo.Delete(ctx, item.ID))

    _, err := repo.Get(ctx, item.ID)
    assert.ErrorIs(t, err, domain.ErrNotFound)
}

func TestContextRepo_Update(t *testing.T) {
    repo, _ := setupRepo(t)
    ctx := context.Background()
    item := newItem(t, domain.ScopeUser, domain.KindNote, domain.SourceManual)
    require.NoError(t, repo.Create(ctx, item))

    item.Title = "Updated"
    item.Content = "New content"
    require.NoError(t, repo.Update(ctx, item))

    got, err := repo.Get(ctx, item.ID)
    require.NoError(t, err)
    assert.Equal(t, "Updated", got.Title)
    assert.Equal(t, "New content", got.Content)
}

func TestContextRepo_ListWithFilter(t *testing.T) {
    repo, _ := setupRepo(t)
    ctx := context.Background()

    for _, k := range []domain.Kind{domain.KindNote, domain.KindNote, domain.KindLink} {
        item := newItem(t, domain.ScopeUser, k, domain.SourceManual)
        if k == domain.KindLink {
            item.Title = "Link"
        }
        require.NoError(t, repo.Create(ctx, item))
    }

    items, _, err := repo.List(ctx, port.ItemFilter{
        Scopes: []domain.Scope{domain.ScopeUser},
        Kinds:  []domain.Kind{domain.KindNote},
        Limit:  10,
    })
    require.NoError(t, err)
    assert.Len(t, items, 2)
}

func TestContextRepo_CursorPagination(t *testing.T) {
    repo, _ := setupRepo(t)
    ctx := context.Background()
    for i := 0; i < 25; i++ {
        item := newItem(t, domain.ScopeUser, domain.KindNote, domain.SourceManual)
        require.NoError(t, repo.Create(ctx, item))
    }

    page1, cursor, err := repo.List(ctx, port.ItemFilter{
        Scopes: []domain.Scope{domain.ScopeUser}, Limit: 10,
    })
    require.NoError(t, err)
    assert.Len(t, page1, 10)
    assert.NotEmpty(t, cursor)

    page2, _, err := repo.List(ctx, port.ItemFilter{
        Scopes: []domain.Scope{domain.ScopeUser}, Limit: 10, Cursor: cursor,
    })
    require.NoError(t, err)
    assert.Len(t, page2, 10)

    // No overlap
    seen := map[string]bool{}
    for _, it := range append(append([]domain.ContextItem{}, page1...), page2...) {
        require.False(t, seen[it.ID], "duplicate id across pages")
        seen[it.ID] = true
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
CGO_ENABLED=1 go test ./internal/adapter/sqlite/...
```

Expected: FAIL — `NewContextRepo` undefined.

- [ ] **Step 3: Write repo.go**

Create `internal/adapter/sqlite/repo.go`:

```go
package sqlite

import (
    "context"
    "database/sql"
    "encoding/json"
    "errors"
    "fmt"
    "strconv"
    "strings"

    "uni-context/internal/domain"
    "uni-context/internal/port"
)

type ContextRepo struct {
    db *sql.DB
}

func NewContextRepo(db *sql.DB) *ContextRepo {
    return &ContextRepo{db: db}
}

const insertItemSQL = `
INSERT INTO context_item (
    id, scope, kind, source, owner_user_id, project_id, agent_id,
    conversation_id, parent_id, title, summary, content, content_uri,
    content_mime, content_hash, language, tags, source_meta, visibility,
    confidence, word_count, any_embedding, created_at, updated_at, version
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
`

func (r *ContextRepo) Create(ctx context.Context, item domain.ContextItem) error {
    tags, err := json.Marshal(item.Tags)
    if err != nil {
        return fmt.Errorf("marshal tags: %w", err)
    }
    meta, err := json.Marshal(item.SourceMeta)
    if err != nil {
        return fmt.Errorf("marshal source_meta: %w", err)
    }
    _, err = r.db.ExecContext(ctx, insertItemSQL,
        item.ID, string(item.Scope), string(item.Kind), string(item.Source),
        nullable(item.OwnerUserID), nullable(item.ProjectID), nullable(item.AgentID),
        nullable(item.ConversationID), nullable(item.ParentID),
        item.Title, item.Summary, item.Content,
        nullable(item.ContentURI), nullable(item.ContentMIME), nullable(item.ContentHash),
        nullable(item.Language), string(tags), string(meta), string(item.Visibility),
        item.Confidence, item.WordCount, item.AnyEmbedding,
        item.CreatedAt.Unix(), item.UpdatedAt.Unix(), item.Version,
    )
    if err != nil {
        return fmt.Errorf("insert item: %w", err)
    }
    return nil
}

const getItemSQL = `
SELECT id, scope, kind, source, owner_user_id, project_id, agent_id,
       conversation_id, parent_id, title, summary, content, content_uri,
       content_mime, content_hash, language, tags, source_meta, visibility,
       confidence, word_count, any_embedding, created_at, updated_at, version
FROM context_item WHERE id = ?
`

func (r *ContextRepo) Get(ctx context.Context, id string) (domain.ContextItem, error) {
    row := r.db.QueryRowContext(ctx, getItemSQL, id)
    item, err := scanItem(row.Scan)
    if errors.Is(err, sql.ErrNoRows) {
        return domain.ContextItem{}, fmt.Errorf("%w: item %s", domain.ErrNotFound, id)
    }
    return item, err
}

func (r *ContextRepo) Update(ctx context.Context, item domain.ContextItem) error {
    tags, _ := json.Marshal(item.Tags)
    meta, _ := json.Marshal(item.SourceMeta)
    item.Version++
    item.UpdatedAt = item.UpdatedAt.UTC()
    res, err := r.db.ExecContext(ctx, `
        UPDATE context_item SET
            title=?, summary=?, content=?, content_uri=?, content_mime=?,
            content_hash=?, language=?, tags=?, source_meta=?, visibility=?,
            confidence=?, word_count=?, updated_at=?, version=?
        WHERE id=?`,
        item.Title, item.Summary, item.Content,
        nullable(item.ContentURI), nullable(item.ContentMIME), nullable(item.ContentHash),
        nullable(item.Language), string(tags), string(meta), string(item.Visibility),
        item.Confidence, item.WordCount, item.UpdatedAt.Unix(), item.Version, item.ID,
    )
    if err != nil {
        return fmt.Errorf("update item: %w", err)
    }
    n, _ := res.RowsAffected()
    if n == 0 {
        return fmt.Errorf("%w: item %s", domain.ErrNotFound, item.ID)
    }
    return nil
}

func (r *ContextRepo) Delete(ctx context.Context, id string) error {
    res, err := r.db.ExecContext(ctx, `DELETE FROM context_item WHERE id=?`, id)
    if err != nil {
        return fmt.Errorf("delete item: %w", err)
    }
    n, _ := res.RowsAffected()
    if n == 0 {
        return fmt.Errorf("%w: item %s", domain.ErrNotFound, id)
    }
    return nil
}

func (r *ContextRepo) List(ctx context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
    if f.Limit <= 0 || f.Limit > 200 {
        f.Limit = 50
    }

    var (
        where []string
        args  []any
    )
    if len(f.Scopes) > 0 {
        where = append(where, "scope IN ("+placeholders(len(f.Scopes))+")")
        for _, s := range f.Scopes {
            args = append(args, string(s))
        }
    }
    if len(f.Kinds) > 0 {
        where = append(where, "kind IN ("+placeholders(len(f.Kinds))+")")
        for _, k := range f.Kinds {
            args = append(args, string(k))
        }
    }
    if f.OwnerUserID != "" {
        where = append(where, "owner_user_id=?")
        args = append(args, f.OwnerUserID)
    }
    if f.ProjectID != "" {
        where = append(where, "project_id=?")
        args = append(args, f.ProjectID)
    }
    if f.Cursor != "" {
        ts, id, err := decodeCursor(f.Cursor)
        if err != nil {
            return nil, "", fmt.Errorf("decode cursor: %w", err)
        }
        where = append(where, "(created_at < ? OR (created_at = ? AND id < ?))")
        args = append(args, ts, ts, id)
    }
    where = append(where, "1=1")

    query := fmt.Sprintf(`
        SELECT id, scope, kind, source, owner_user_id, project_id, agent_id,
               conversation_id, parent_id, title, summary, content, content_uri,
               content_mime, content_hash, language, tags, source_meta, visibility,
               confidence, word_count, any_embedding, created_at, updated_at, version
        FROM context_item
        WHERE %s
        ORDER BY created_at DESC, id DESC
        LIMIT ?`, strings.Join(where, " AND "))
    args = append(args, f.Limit+1) // +1 to detect next page

    rows, err := r.db.QueryContext(ctx, query, args...)
    if err != nil {
        return nil, "", fmt.Errorf("list items: %w", err)
    }
    defer rows.Close()

    var items []domain.ContextItem
    for rows.Next() {
        item, err := scanItem(rows.Scan)
        if err != nil {
            return nil, "", err
        }
        items = append(items, item)
    }
    if err := rows.Err(); err != nil {
        return nil, "", err
    }

    var nextCursor string
    if len(items) > f.Limit {
        items = items[:f.Limit]
        nextCursor = r.NextCursor(items[len(items)-1])
    }
    return items, nextCursor, nil
}

func (r *ContextRepo) NextCursor(item domain.ContextItem) string {
    return encodeCursor(item.CreatedAt.Unix(), item.ID)
}

// --- helpers ---

func nullable(s string) any {
    if s == "" {
        return nil
    }
    return s
}

func placeholders(n int) string {
    return strings.Repeat("?,", n-1) + "?"
}

type scanFn func(...any) error

func scanItem(scan scanFn) (domain.ContextItem, error) {
    var (
        item         domain.ContextItem
        scope        string
        kind         string
        source       string
        owner        sql.NullString
        project      sql.NullString
        agent        sql.NullString
        conv         sql.NullString
        parent       sql.NullString
        contentURI   sql.NullString
        contentMIME  sql.NullString
        contentHash  sql.NullString
        language     sql.NullString
        tags         string
        meta         string
        visibility   string
        createdAt    int64
        updatedAt    int64
    )
    err := scan(
        &item.ID, &scope, &kind, &source, &owner, &project, &agent,
        &conv, &parent, &item.Title, &item.Summary, &item.Content,
        &contentURI, &contentMIME, &contentHash, &language,
        &tags, &meta, &visibility, &item.Confidence, &item.WordCount,
        &item.AnyEmbedding, &createdAt, &updatedAt, &item.Version,
    )
    if err != nil {
        return domain.ContextItem{}, err
    }
    item.Scope = domain.Scope(scope)
    item.Kind = domain.Kind(kind)
    item.Source = domain.Source(source)
    item.OwnerUserID = owner.String
    item.ProjectID = project.String
    item.AgentID = agent.String
    item.ConversationID = conv.String
    item.ParentID = parent.String
    item.ContentURI = contentURI.String
    item.ContentMIME = contentMIME.String
    item.ContentHash = contentHash.String
    item.Language = language.String
    item.Visibility = domain.Visibility(visibility)
    item.CreatedAt = unixToTime(createdAt)
    item.UpdatedAt = unixToTime(updatedAt)
    if err := json.Unmarshal([]byte(tags), &item.Tags); err != nil {
        return domain.ContextItem{}, fmt.Errorf("unmarshal tags: %w", err)
    }
    if item.Tags == nil {
        item.Tags = []string{}
    }
    if err := json.Unmarshal([]byte(meta), &item.SourceMeta); err != nil {
        return domain.ContextItem{}, fmt.Errorf("unmarshal source_meta: %w", err)
    }
    if item.SourceMeta == nil {
        item.SourceMeta = map[string]any{}
    }
    return item, nil
}

func encodeCursor(ts int64, id string) string {
    // simple "ts:id" base64-urlencoded
    return strconv.FormatInt(ts, 36) + ":" + id
}

func decodeCursor(c string) (int64, string, error) {
    parts := strings.SplitN(c, ":", 2)
    if len(parts) != 2 {
        return 0, "", errors.New("malformed cursor")
    }
    ts, err := strconv.ParseInt(parts[0], 36, 64)
    if err != nil {
        return 0, "", err
    }
    return ts, parts[1], nil
}

func unixToTime(ts int64) (t timeT) {
    return timeFromUnix(ts)
}
```

Create a small helper file `internal/adapter/sqlite/time.go`:

```go
package sqlite

import "time"

type timeT = time.Time

func timeFromUnix(ts int64) time.Time {
    return time.Unix(ts, 0).UTC()
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
CGO_ENABLED=1 go test -race ./internal/adapter/sqlite/...
```

Expected: all tests in repo_test.go PASS.

- [ ] **Step 5: Write project_repo_test.go**

Create `internal/adapter/sqlite/project_repo_test.go`:

```go
package sqlite

import (
    "context"
    "database/sql"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
    "uni-context/internal/domain"
    "uni-context/internal/port"
)

func setupProjectRepo(t *testing.T) port.ProjectRepo {
    t.Helper()
    db, err := sql.Open("sqlite3", ":memory:")
    require.NoError(t, err)
    require.NoError(t, Migrate(db))
    t.Cleanup(func() { db.Close() })
    return NewProjectRepo(db)
}

func TestProjectRepo_CRUD(t *testing.T) {
    repo := setupProjectRepo(t)
    ctx := context.Background()

    p, err := domain.NewProject("my-app", "/path/to/app", "test project")
    require.NoError(t, err)
    require.NoError(t, repo.Create(ctx, p))

    got, err := repo.GetByName(ctx, "my-app")
    require.NoError(t, err)
    assert.Equal(t, p.ID, got.ID)

    list, err := repo.List(ctx)
    require.NoError(t, err)
    assert.Len(t, list, 1)

    require.NoError(t, repo.Delete(ctx, p.ID))
    _, err = repo.GetByName(ctx, "my-app")
    assert.ErrorIs(t, err, domain.ErrNotFound)
}
```

- [ ] **Step 6: Write project_repo.go**

Create `internal/adapter/sqlite/project_repo.go`:

```go
package sqlite

import (
    "context"
    "database/sql"
    "errors"
    "fmt"

    "uni-context/internal/domain"
    "uni-context/internal/port"
)

type ProjectRepo struct {
    db *sql.DB
}

func NewProjectRepo(db *sql.DB) *ProjectRepo {
    return &ProjectRepo{db: db}
}

func (r *ProjectRepo) Create(ctx context.Context, p domain.Project) error {
    _, err := r.db.ExecContext(ctx, `
        INSERT INTO project (id, name, path, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)`,
        p.ID, p.Name, nullable(p.Path), nullable(p.Description),
        p.CreatedAt.Unix(), p.UpdatedAt.Unix(),
    )
    if err != nil {
        return fmt.Errorf("insert project: %w", err)
    }
    return nil
}

func (r *ProjectRepo) GetByName(ctx context.Context, name string) (domain.Project, error) {
    var p domain.Project
    var path, desc sql.NullString
    var created, updated int64
    err := r.db.QueryRowContext(ctx,
        `SELECT id, name, path, description, created_at, updated_at FROM project WHERE name=?`,
        name,
    ).Scan(&p.ID, &p.Name, &path, &desc, &created, &updated)
    if errors.Is(err, sql.ErrNoRows) {
        return domain.Project{}, fmt.Errorf("%w: project %s", domain.ErrNotFound, name)
    }
    if err != nil {
        return domain.Project{}, fmt.Errorf("get project: %w", err)
    }
    p.Path = path.String
    p.Description = desc.String
    p.CreatedAt = timeFromUnix(created)
    p.UpdatedAt = timeFromUnix(updated)
    return p, nil
}

func (r *ProjectRepo) List(ctx context.Context) ([]domain.Project, error) {
    rows, err := r.db.QueryContext(ctx,
        `SELECT id, name, path, description, created_at, updated_at FROM project ORDER BY name`,
    )
    if err != nil {
        return nil, fmt.Errorf("list projects: %w", err)
    }
    defer rows.Close()
    var out []domain.Project
    for rows.Next() {
        var p domain.Project
        var path, desc sql.NullString
        var created, updated int64
        if err := rows.Scan(&p.ID, &p.Name, &path, &desc, &created, &updated); err != nil {
            return nil, err
        }
        p.Path = path.String
        p.Description = desc.String
        p.CreatedAt = timeFromUnix(created)
        p.UpdatedAt = timeFromUnix(updated)
        out = append(out, p)
    }
    return out, rows.Err()
}

func (r *ProjectRepo) Delete(ctx context.Context, id string) error {
    res, err := r.db.ExecContext(ctx, `DELETE FROM project WHERE id=?`, id)
    if err != nil {
        return fmt.Errorf("delete project: %w", err)
    }
    n, _ := res.RowsAffected()
    if n == 0 {
        return fmt.Errorf("%w: project %s", domain.ErrNotFound, id)
    }
    return nil
}
```

- [ ] **Step 7: Run all tests**

```bash
CGO_ENABLED=1 go test -race ./internal/adapter/sqlite/...
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add internal/adapter/sqlite/
git commit -m "feat(adapter/sqlite): implement ContextRepo and ProjectRepo"
```

---

## Task 6: SQLite FTS Searcher

**Files:**
- Create: `internal/adapter/sqlite/searcher.go`, `internal/adapter/sqlite/searcher_test.go`

**Interfaces:**
- Consumes: `port.Searcher` / `port.SearchQuery`
- Produces: `sqlite.NewSearcher(db)` — BM25 search via FTS5 trigram index.

- [ ] **Step 1: Write the failing test**

Create `internal/adapter/sqlite/searcher_test.go`:

```go
package sqlite

import (
    "context"
    "database/sql"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
    "uni-context/internal/domain"
    "uni-context/internal/port"
)

func TestSearcher_FTS_BasicMatch(t *testing.T) {
    db := openMemWithSampleData(t, []domain.ContextItem{
        makeItem("如何部署 Go 服务到 k8s", "k8s deployment yaml 示例"),
        makeItem("向量数据库选型对比", "Qdrant vs sqlite-vec"),
        makeItem("Python 部署 Flask 应用", "gunicorn + nginx"),
    })
    s := NewSearcher(db)

    hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "部署", Limit: 10})
    require.NoError(t, err)
    require.Len(t, hits, 2, "both 部署-related items should match")
    assert.Contains(t, []string{"如何部署 Go 服务到 k8s", "Python 部署 Flask 应用"}, hits[0].Snippet+hits[1].Snippet)
}

func TestSearcher_FTS_CJKTrigram(t *testing.T) {
    db := openMemWithSampleData(t, []domain.ContextItem{
        makeItem("部署文档", "如何部署"),
        makeItem("上线流程", "与部署无关"),
    })
    s := NewSearcher(db)

    // trigram tokenizer should match 部署 inside longer Chinese strings
    hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "部署", Limit: 5})
    require.NoError(t, err)
    assert.GreaterOrEqual(t, len(hits), 1)
}

func TestSearcher_FTS_RankingBM25(t *testing.T) {
    db := openMemWithSampleData(t, []domain.ContextItem{
        makeItem("部署 部署 部署", "部署部署部署"),
        makeItem("部署", "无关内容"),
    })
    s := NewSearcher(db)
    hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "部署", Limit: 5})
    require.NoError(t, err)
    require.GreaterOrEqual(t, len(hits), 2)
    // Higher-frequency match should rank first
    assert.True(t, hits[0].Score >= hits[1].Score)
}

func TestSearcher_FTS_NoMatch(t *testing.T) {
    db := openMemWithSampleData(t, []domain.ContextItem{makeItem("hello", "world")})
    s := NewSearcher(db)
    hits, err := s.SearchFTS(context.Background(), port.SearchQuery{Query: "nonexistent", Limit: 5})
    require.NoError(t, err)
    assert.Empty(t, hits)
}

func makeItem(title, content string) domain.ContextItem {
    item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
        domain.NewItemParams{OwnerUserID: "u"})
    item.Title = title
    item.Content = content
    return item
}

func openMemWithSampleData(t *testing.T, items []domain.ContextItem) *sql.DB {
    t.Helper()
    db, err := sql.Open("sqlite3", ":memory:")
    require.NoError(t, err)
    require.NoError(t, Migrate(db))
    t.Cleanup(func() { db.Close() })
    repo := NewContextRepo(db)
    for _, it := range items {
        require.NoError(t, repo.Create(context.Background(), it))
    }
    return db
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
CGO_ENABLED=1 go test ./internal/adapter/sqlite/... -run Searcher
```

Expected: FAIL — `NewSearcher` undefined.

- [ ] **Step 3: Write searcher.go**

Create `internal/adapter/sqlite/searcher.go`:

```go
package sqlite

import (
    "context"
    "database/sql"
    "fmt"
    "strings"

    "uni-context/internal/port"
)

type Searcher struct {
    db *sql.DB
}

func NewSearcher(db *sql.DB) *Searcher {
    return &Searcher{db: db}
}

// ftsQueryString builds a safe FTS5 query: wrap each token in double quotes
// and join with AND. This avoids FTS5 operator injection.
// For CJK (trigram), the raw substring works because trigram indexes all 3-grams.
func ftsQueryString(raw string) string {
    raw = strings.TrimSpace(raw)
    if raw == "" {
        return ""
    }
    // Treat as phrase match (escaped).
    escaped := strings.ReplaceAll(raw, `"`, `""`)
    return `"` + escaped + `"`
}

const searchSQL = `
SELECT ci.id, bm25(context_fts) AS score,
       snippet(context_fts, 2, '<b>', '</b>', '…', 16) AS snip
FROM context_fts
JOIN context_item ci ON ci.rowid = context_fts.rowid
WHERE context_fts MATCH ?
ORDER BY bm25(context_fts)
LIMIT ?
`

func (s *Searcher) SearchFTS(ctx context.Context, q port.SearchQuery) ([]port.SearchHit, error) {
    ftsq := ftsQueryString(q.Query)
    if ftsq == "" {
        return nil, nil
    }
    limit := q.Limit
    if limit <= 0 || limit > 200 {
        limit = 20
    }

    rows, err := s.db.QueryContext(ctx, searchSQL, ftsq, limit)
    if err != nil {
        return nil, fmt.Errorf("fts search: %w", err)
    }
    defer rows.Close()

    var hits []port.SearchHit
    for rows.Next() {
        var h port.SearchHit
        if err := rows.Scan(&h.ID, &h.Score, &h.Snippet); err != nil {
            return nil, err
        }
        // bm25 returns negative scores (lower = better). Negate so higher = better.
        h.Score = -h.Score
        hits = append(hits, h)
    }
    return hits, rows.Err()
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
CGO_ENABLED=1 go test -race ./internal/adapter/sqlite/... -run Searcher
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/adapter/sqlite/searcher.go internal/adapter/sqlite/searcher_test.go
git commit -m "feat(adapter/sqlite): implement FTS5 BM25 searcher"
```

---

## Task 7: fsstore Adapter

**Files:**
- Create: `internal/adapter/fsstore/store.go`, `internal/adapter/fsstore/store_test.go`

**Interfaces:**
- Consumes: `port.FileStore`
- Produces: `fsstore.New(rootDir)` — hash-addressed file storage with refcount metadata.

- [ ] **Step 1: Write the failing test**

Create `internal/adapter/fsstore/store_test.go`:

```go
package fsstore

import (
    "os"
    "path/filepath"
    "strings"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
)

func TestFileStore_PutAndGet(t *testing.T) {
    root := t.TempDir()
    s, err := New(root)
    require.NoError(t, err)

    content := []byte("hello world this is a test")
    uri, hash, err := s.Put(content, "text/plain")
    require.NoError(t, err)
    assert.True(t, strings.HasPrefix(hash, "sha256:"))
    assert.True(t, strings.HasPrefix(uri, "file://"))

    got, err := s.Get(uri)
    require.NoError(t, err)
    assert.Equal(t, content, got)
}

func TestFileStore_PutDeduplicates(t *testing.T) {
    root := t.TempDir()
    s, _ := New(root)

    content := []byte("same content same hash")
    uri1, hash1, _ := s.Put(content, "text/plain")
    uri2, hash2, _ := s.Put(content, "text/plain")

    assert.Equal(t, uri1, uri2)
    assert.Equal(t, hash1, hash2)

    // File exists exactly once on disk
    files, _ := filepath.Glob(filepath.Join(root, "*", hash1[len("sha256:"):]))
    assert.Len(t, files, 1)
}

func TestFileStore_DeleteRefcount(t *testing.T) {
    root := t.TempDir()
    s, _ := New(root)

    content := []byte("to be deleted")
    uri, _, _ := s.Put(content, "text/plain")
    // First delete on a single-ref content removes the file
    require.NoError(t, s.Delete(uri))
    _, err := s.Get(uri)
    assert.Error(t, err)
    _ = os.Stderr // suppress unused
}

func TestFileStore_GetMissingReturnsError(t *testing.T) {
    root := t.TempDir()
    s, _ := New(root)
    _, err := s.Get("file://nonexistent")
    assert.Error(t, err)
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
CGO_ENABLED=1 go test ./internal/adapter/fsstore/...
```

Expected: FAIL — `fsstore.New` undefined.

- [ ] **Step 3: Write store.go**

Create `internal/adapter/fsstore/store.go`:

```go
package fsstore

import (
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "errors"
    "fmt"
    "os"
    "path/filepath"
    "strings"
    "sync"
)

type FileStore struct {
    root string
    mu   sync.Mutex
}

func New(root string) (*FileStore, error) {
    if err := os.MkdirAll(root, 0o755); err != nil {
        return nil, fmt.Errorf("create filestore root: %w", err)
    }
    return &FileStore{root: root}, nil
}

func (s *FileStore) Put(content []byte, mime string) (uri string, hash string, err error) {
    sum := sha256.Sum256(content)
    hex := hex.EncodeToString(sum[:])
    hash = "sha256:" + hex
    bucket := hex[:2]
    dir := filepath.Join(s.root, bucket)
    if err := os.MkdirAll(dir, 0o755); err != nil {
        return "", "", fmt.Errorf("mkdir bucket: %w", err)
    }
    contentPath := filepath.Join(dir, hex)
    metaPath := contentPath + ".meta"

    s.mu.Lock()
    defer s.mu.Unlock()

    // Idempotent: if file exists, just bump refcount.
    if _, err := os.Stat(contentPath); err == nil {
        if err := s.bumpRefcount(metaPath, +1); err != nil {
            return "", "", err
        }
        return s.uriFor(hex), hash, nil
    }

    if err := os.WriteFile(contentPath, content, 0o644); err != nil {
        return "", "", fmt.Errorf("write content: %w", err)
    }
    if err := s.writeMeta(metaPath, 1, mime, len(content)); err != nil {
        _ = os.Remove(contentPath)
        return "", "", err
    }
    return s.uriFor(hex), hash, nil
}

func (s *FileStore) Get(uri string) ([]byte, error) {
    hex, err := s.hashFromURI(uri)
    if err != nil {
        return nil, err
    }
    path := s.pathFor(hex)
    data, err := os.ReadFile(path)
    if err != nil {
        if errors.Is(err, os.ErrNotExist) {
            return nil, fmt.Errorf("content not found: %s", hex)
        }
        return nil, err
    }
    return data, nil
}

func (s *FileStore) Delete(uri string) error {
    hex, err := s.hashFromURI(uri)
    if err != nil {
        return err
    }
    contentPath := s.pathFor(hex)
    metaPath := contentPath + ".meta"

    s.mu.Lock()
    defer s.mu.Unlock()

    meta, err := s.readMeta(metaPath)
    if err != nil {
        return err
    }
    meta.RefCount--
    if meta.RefCount > 0 {
        return s.writeMeta(metaPath, meta.RefCount, meta.MIME, meta.Size)
    }
    if err := os.Remove(contentPath); err != nil && !errors.Is(err, os.ErrNotExist) {
        return err
    }
    if err := os.Remove(metaPath); err != nil && !errors.Is(err, os.ErrNotExist) {
        return err
    }
    return nil
}

func (s *FileStore) uriFor(hex string) string {
    return "file://" + hex
}

func (s *FileStore) pathFor(hex string) string {
    return filepath.Join(s.root, hex[:2], hex)
}

func (s *FileStore) hashFromURI(uri string) (string, error) {
    if !strings.HasPrefix(uri, "file://") {
        return "", fmt.Errorf("unsupported uri scheme: %s", uri)
    }
    hex := strings.TrimPrefix(uri, "file://")
    if len(hex) != 64 { // sha256 hex length
        return "", fmt.Errorf("malformed hash in uri: %s", uri)
    }
    return hex, nil
}

type meta struct {
    RefCount int    `json:"refcount"`
    MIME     string `json:"mime"`
    Size     int    `json:"size"`
}

func (s *FileStore) readMeta(path string) (meta, error) {
    data, err := os.ReadFile(path)
    if err != nil {
        return meta{}, fmt.Errorf("read meta: %w", err)
    }
    var m meta
    if err := json.Unmarshal(data, &m); err != nil {
        return meta{}, fmt.Errorf("unmarshal meta: %w", err)
    }
    return m, nil
}

func (s *FileStore) writeMeta(path string, refcount int, mime string, size int) error {
    m := meta{RefCount: refcount, MIME: mime, Size: size}
    data, _ := json.Marshal(m)
    return os.WriteFile(path, data, 0o644)
}

func (s *FileStore) bumpRefcount(path string, delta int) error {
    m, err := s.readMeta(path)
    if err != nil {
        return err
    }
    m.RefCount += delta
    if m.RefCount < 0 {
        m.RefCount = 0
    }
    return s.writeMeta(path, m.RefCount, m.MIME, m.Size)
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
CGO_ENABLED=1 go test -race ./internal/adapter/fsstore/...
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/adapter/fsstore/
git commit -m "feat(adapter/fsstore): implement hash-addressed file storage with refcount"
```

---

## Task 8: Ingest Service

**Files:**
- Create: `internal/service/ingest.go`, `internal/service/ingest_test.go`

**Interfaces:**
- Consumes: `port.ContextRepo`, `port.FileStore`, `domain.ContextItem`
- Produces: `service.IngestService` with `Create(ctx, Input) (id, error)` method. Applies validation, content externalization (>4KB), word count, filestore dedup.

- [ ] **Step 1: Write the failing test**

Create `internal/service/ingest_test.go`:

```go
package service

import (
    "context"
    "strings"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
    "uni-context/internal/domain"
)

func TestIngest_Create_SmallContentInline(t *testing.T) {
    f := newIngestFixture(t)
    id, err := f.svc.Create(context.Background(), Input{
        Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
        OwnerUserID: "u-1",
        Title: "Test", Content: "small content",
        Tags: []string{"t1"},
    })
    require.NoError(t, err)
    assert.NotEmpty(t, id)

    got, err := f.repo.Get(context.Background(), id)
    require.NoError(t, err)
    assert.Equal(t, "Test", got.Title)
    assert.Equal(t, "small content", got.Content)
    assert.Empty(t, got.ContentURI)
    assert.Equal(t, []string{"t1"}, got.Tags)
    assert.Greater(t, got.WordCount, 0)
}

func TestIngest_Create_LargeContentExternalized(t *testing.T) {
    f := newIngestFixture(t)
    large := strings.Repeat("word ", 1000) // ~5KB
    id, err := f.svc.Create(context.Background(), Input{
        Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
        OwnerUserID: "u-1", Content: large,
    })
    require.NoError(t, err)

    got, _ := f.repo.Get(context.Background(), id)
    assert.Empty(t, got.Content, "inline content should be emptied")
    assert.NotEmpty(t, got.ContentURI, "content_uri should be set")
    assert.Contains(t, got.ContentURI, "file://")
    assert.NotEmpty(t, got.ContentHash)

    // FileStore can resolve the content
    data, err := f.fs.Get(got.ContentURI)
    require.NoError(t, err)
    assert.Equal(t, large, string(data))
}

func TestIngest_Create_RejectsInvalidScope(t *testing.T) {
    f := newIngestFixture(t)
    _, err := f.svc.Create(context.Background(), Input{
        Scope: domain.ScopeGlobal, Kind: domain.KindNote, Source: domain.SourceManual,
        OwnerUserID: "u-1", // invalid with global
    })
    require.Error(t, err)
    assert.ErrorIs(t, err, domain.ErrValidation)
}

func TestIngest_Create_DeduplicatesByContentHash(t *testing.T) {
    f := newIngestFixture(t)
    content := strings.Repeat("a", 5000)
    id1, err := f.svc.Create(context.Background(), Input{
        Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
        OwnerUserID: "u-1", Content: content,
    })
    require.NoError(t, err)

    id2, err := f.svc.Create(context.Background(), Input{
        Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
        OwnerUserID: "u-1", Content: content,
    })
    require.NoError(t, err)

    // Two items, same hash, single filestore entry
    assert.NotEqual(t, id1, id2)
    got1, _ := f.repo.Get(context.Background(), id1)
    got2, _ := f.repo.Get(context.Background(), id2)
    assert.Equal(t, got1.ContentHash, got2.ContentHash)
    assert.Equal(t, got1.ContentURI, got2.ContentURI)
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
CGO_ENABLED=1 go test ./internal/service/...
```

Expected: FAIL — service types undefined.

- [ ] **Step 3: Write ingest.go**

Create `internal/service/ingest.go`:

```go
package service

import (
    "context"
    "fmt"
    "strings"
    "unicode"

    "uni-context/internal/domain"
    "uni-context/internal/port"
)

type IngestService struct {
    repo port.ContextRepo
    fs   port.FileStore
}

func NewIngestService(repo port.ContextRepo, fs port.FileStore) *IngestService {
    return &IngestService{repo: repo, fs: fs}
}

// Input is the user-facing write request.
type Input struct {
    Scope       domain.Scope
    Kind        domain.Kind
    Source      domain.Source
    OwnerUserID string
    ProjectID   string
    AgentID     string

    Title   string
    Summary string
    Content string
    Tags    []string

    SourceMeta map[string]any
}

func (s *IngestService) Create(ctx context.Context, in Input) (string, error) {
    item, err := domain.NewContextItem(in.Scope, in.Kind, in.Source, domain.NewItemParams{
        OwnerUserID: in.OwnerUserID,
        ProjectID:   in.ProjectID,
        AgentID:     in.AgentID,
    })
    if err != nil {
        return "", err
    }

    item.Title = strings.TrimSpace(in.Title)
    item.Summary = in.Summary
    item.Tags = in.Tags
    if item.Tags == nil {
        item.Tags = []string{}
    }
    item.SourceMeta = in.SourceMeta
    if item.SourceMeta == nil {
        item.SourceMeta = map[string]any{}
    }
    item.WordCount = countWords(in.Content)

    if len(in.Content) > domain.ContentInlineLimit {
        uri, hash, err := s.fs.Put([]byte(in.Content), "text/plain")
        if err != nil {
            return "", fmt.Errorf("externalize content: %w", err)
        }
        item.ContentURI = uri
        item.ContentHash = hash
        item.ContentMIME = "text/plain"
        item.Content = ""
    } else {
        item.Content = in.Content
    }

    if err := s.repo.Create(ctx, item); err != nil {
        return "", fmt.Errorf("persist item: %w", err)
    }
    return item.ID, nil
}

func countWords(s string) int {
    n := 0
    inWord := false
    for _, r := range s {
        if unicode.IsSpace(r) {
            inWord = false
            continue
        }
        if !inWord {
            n++
            inWord = true
        }
    }
    return n
}
```

Now create the test fixture file `internal/service/fixture_test.go`:

```go
package service

import (
    "os"
    "path/filepath"
    "testing"

    "github.com/stretchr/testify/require"
    "uni-context/internal/adapter/fsstore"
    "uni-context/internal/port"
)

// fakeRepo is an in-memory port.ContextRepo for service tests.
// (For adapter-level tests, see the sqlite package.)
// We hand-roll this rather than use a mock generator — the interface is small.

type ingestFixture struct {
    repo *fakeRepo
    fs   port.FileStore
    svc  *IngestService
}

func newIngestFixture(t *testing.T) *ingestFixture {
    t.Helper()
    repo := newFakeRepo()
    root := filepath.Join(t.TempDir(), "fs")
    fs, err := fsstore.New(root)
    require.NoError(t, err)
    return &ingestFixture{
        repo: repo,
        fs:   fs,
        svc:  NewIngestService(repo, fs),
    }
}

// suppress unused-import in case os gets dropped
var _ = os.Stderr
```

Create `internal/service/fake_repo_test.go`:

```go
package service

import (
    "context"
    "fmt"
    "sync"

    "uni-context/internal/domain"
    "uni-context/internal/port"
)

type fakeRepo struct {
    mu    sync.Mutex
    items map[string]domain.ContextItem
}

func newFakeRepo() *fakeRepo {
    return &fakeRepo{items: map[string]domain.ContextItem{}}
}

func (r *fakeRepo) Create(_ context.Context, item domain.ContextItem) error {
    r.mu.Lock()
    defer r.mu.Unlock()
    if _, exists := r.items[item.ID]; exists {
        return fmt.Errorf("duplicate id")
    }
    r.items[item.ID] = item
    return nil
}

func (r *fakeRepo) Get(_ context.Context, id string) (domain.ContextItem, error) {
    r.mu.Lock()
    defer r.mu.Unlock()
    item, ok := r.items[id]
    if !ok {
        return domain.ContextItem{}, fmt.Errorf("%w: %s", domain.ErrNotFound, id)
    }
    return item, nil
}

func (r *fakeRepo) Update(_ context.Context, item domain.ContextItem) error {
    r.mu.Lock()
    defer r.mu.Unlock()
    if _, ok := r.items[item.ID]; !ok {
        return fmt.Errorf("%w: %s", domain.ErrNotFound, item.ID)
    }
    r.items[item.ID] = item
    return nil
}

func (r *fakeRepo) Delete(_ context.Context, id string) error {
    r.mu.Lock()
    defer r.mu.Unlock()
    if _, ok := r.items[id]; !ok {
        return fmt.Errorf("%w: %s", domain.ErrNotFound, id)
    }
    delete(r.items, id)
    return nil
}

func (r *fakeRepo) List(_ context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
    r.mu.Lock()
    defer r.mu.Unlock()
    out := make([]domain.ContextItem, 0, len(r.items))
    for _, it := range r.items {
        out = append(out, it)
    }
    return out, "", nil
}

func (r *fakeRepo) NextCursor(_ domain.ContextItem) string { return "" }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
CGO_ENABLED=1 go test -race ./internal/service/...
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/service/
git commit -m "feat(service): implement IngestService with content externalization"
```

---

## Task 9: Search Service

**Files:**
- Create: `internal/service/search.go`, `internal/service/search_test.go`

**Interfaces:**
- Consumes: `port.Searcher`, `port.ContextRepo`
- Produces: `service.SearchService` with `Search(ctx, SearchRequest) (SearchResponse, error)`. Wraps FTS results with item hydration and filtering.

- [ ] **Step 1: Write the failing test**

Create `internal/service/search_test.go`:

```go
package service

import (
    "context"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
    "uni-context/internal/domain"
    "uni-context/internal/port"
)

func TestSearchService_HydratesResults(t *testing.T) {
    f := newSearchFixture(t)
    seedID, _ := f.ingest.Create(context.Background(), Input{
        Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
        OwnerUserID: "u-1", Title: "Note", Content: "searchable text here",
    })

    resp, err := f.svc.Search(context.Background(), SearchRequest{
        Query: "searchable", Limit: 10,
    })
    require.NoError(t, err)
    require.Len(t, resp.Results, 1)
    assert.Equal(t, seedID, resp.Results[0].Item.ID)
    assert.NotEmpty(t, resp.Results[0].Snippet)
    assert.Greater(t, resp.Results[0].Score, 0.0)
}

func TestSearchService_FiltersByScope(t *testing.T) {
    f := newSearchFixture(t)
    _, _ = f.ingest.Create(context.Background(), Input{
        Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
        OwnerUserID: "u-1", Content: "common keyword",
    })
    _, _ = f.ingest.Create(context.Background(), Input{
        Scope: domain.ScopeGlobal, Kind: domain.KindDoc, Source: domain.SourceImport,
        Content: "common keyword",
    })

    resp, err := f.svc.Search(context.Background(), SearchRequest{
        Query: "common", Scopes: []domain.Scope{domain.ScopeUser},
    })
    require.NoError(t, err)
    for _, r := range resp.Results {
        assert.Equal(t, domain.ScopeUser, r.Item.Scope)
    }
}

// fakeSearcher is a hand-rolled port.Searcher that searches pre-seeded items.
type fakeSearcher struct {
    items []domain.ContextItem
}

func (s *fakeSearcher) SearchFTS(_ context.Context, q port.SearchQuery) ([]port.SearchHit, error) {
    var hits []port.SearchHit
    for _, it := range s.items {
        if contains(it.Content, q.Query) || contains(it.Title, q.Query) {
            hits = append(hits, port.SearchHit{ID: it.ID, Score: 1.0, Snippet: q.Query})
        }
    }
    return hits, nil
}

func contains(s, sub string) bool {
    if sub == "" {
        return false
    }
    for i := 0; i+len(sub) <= len(s); i++ {
        if s[i:i+len(sub)] == sub {
            return true
        }
    }
    return false
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
CGO_ENABLED=1 go test ./internal/service/... -run SearchService
```

Expected: FAIL — `SearchService` undefined.

- [ ] **Step 3: Write search.go**

Create `internal/service/search.go`:

```go
package service

import (
    "context"
    "fmt"

    "uni-context/internal/domain"
    "uni-context/internal/port"
)

type SearchService struct {
    searcher port.Searcher
    repo     port.ContextRepo
}

func NewSearchService(searcher port.Searcher, repo port.ContextRepo) *SearchService {
    return &SearchService{searcher: searcher, repo: repo}
}

type SearchRequest struct {
    Query  string
    Scopes []domain.Scope
    Kinds  []domain.Kind
    Limit  int
}

type SearchResult struct {
    Item    domain.ContextItem
    Score   float64
    Snippet string
}

type SearchResponse struct {
    Results []SearchResult
    Total   int
}

func (s *SearchService) Search(ctx context.Context, req SearchRequest) (SearchResponse, error) {
    hits, err := s.searcher.SearchFTS(ctx, port.SearchQuery{Query: req.Query, Limit: req.Limit})
    if err != nil {
        return SearchResponse{}, fmt.Errorf("fts: %w", err)
    }

    scopes := scopeSet(req.Scopes)
    kinds := kindSet(req.Kinds)

    var out []SearchResult
    for _, h := range hits {
        item, err := s.repo.Get(ctx, h.ID)
        if err != nil {
            // item was deleted between FTS row and now; skip
            continue
        }
        if !scopes[item.Scope] {
            continue
        }
        if !kinds[item.Kind] {
            continue
        }
        out = append(out, SearchResult{Item: item, Score: h.Score, Snippet: h.Snippet})
    }

    return SearchResponse{Results: out, Total: len(out)}, nil
}

func scopeSet(s []domain.Scope) map[domain.Scope]bool {
    if len(s) == 0 {
        return nil // nil map = "all match"
    }
    m := map[domain.Scope]bool{}
    for _, v := range s {
        m[v] = true
    }
    return m
}

func kindSet(k []domain.Kind) map[domain.Kind]bool {
    if len(k) == 0 {
        return nil
    }
    m := map[domain.Kind]bool{}
    for _, v := range k {
        m[v] = true
    }
    return m
}
```

Add the test fixture `internal/service/search_fixture_test.go`:

```go
package service

import (
    "context"
    "testing"

    "github.com/stretchr/testify/require"
    "uni-context/internal/domain"
)

type searchFixture struct {
    ingest *IngestService
    svc    *SearchService
    repo   *fakeRepo
    fs     *fakeFileStoreShim
}

// fakeFileStoreShim wraps the real fsstore for the search fixture so we can
// reuse ingest + a fake searcher without DB.
type fakeFileStoreShim struct {
    inner *fakeFS
}

func newSearchFixture(t *testing.T) *searchFixture {
    t.Helper()
    repo := newFakeRepo()
    fs := &fakeFS{}
    ingest := NewIngestService(repo, fs)
    // pre-collect items into a slice for the fake searcher
    fsKnit := &fakeSearcher{items: nil}
    svc := NewSearchService(fsKnit, repo)

    // Hook: whenever ingest creates, append to searcher's known items
    // We do this by wrapping repo.Create via a spy.
    spy := &spyRepo{inner: repo, onCreate: func(it domain.ContextItem) {
        fsKnit.items = append(fsKnit.items, it)
    }}
    ingest.repo = spy

    return &searchFixture{ingest: ingest, svc: svc, repo: repo, fs: nil}
}

type spyRepo struct {
    inner    port.ContextRepoLike
    onCreate func(domain.ContextItem)
}

// Inline minimal interface to avoid circular aliasing; full port.ContextRepo
// implemented via embedding the inner.
var _ interface{ /* placeholder */ } = nil

// Use type alias trick:
type ContextRepoLike = port.ContextRepo
```

Wait — this is getting tangled. Let me simplify the search test fixture by using the real sqlite-backed repo + searcher. That's a more honest integration test for the service layer.

Replace `internal/service/search_fixture_test.go` with this simpler version:

```go
package service

import (
    "context"
    "database/sql"
    "path/filepath"
    "testing"

    "github.com/stretchr/testify/require"
    "uni-context/internal/adapter/fsstore"
    "uni-context/internal/adapter/sqlite"
)

type searchFixture struct {
    ingest *IngestService
    svc    *SearchService
}

func newSearchFixture(t *testing.T) *searchFixture {
    t.Helper()
    db, err := sql.Open("sqlite3", ":memory:")
    require.NoError(t, err)
    require.NoError(t, sqlite.Migrate(db))
    t.Cleanup(func() { db.Close() })

    repo := sqlite.NewContextRepo(db)
    searcher := sqlite.NewSearcher(db)
    fs, err := fsstore.New(filepath.Join(t.TempDir(), "fs"))
    require.NoError(t, err)

    return &searchFixture{
        ingest: NewIngestService(repo, fs),
        svc:    NewSearchService(searcher, repo),
    }
}

// keep the helpers below to avoid unused-import complaints if the file is
// compiled alone.
var _ = context.Background
```

And delete the `spyRepo` confusion above — just use the real adapters. Update search_test.go's `fakeSearcher` is no longer needed; remove it.

- [ ] **Step 4: Run tests to verify they pass**

```bash
CGO_ENABLED=1 go test -race ./internal/service/...
```

Expected: PASS. Both ingest and search tests green.

- [ ] **Step 5: Commit**

```bash
git add internal/service/
git commit -m "feat(service): implement SearchService wrapping FTS results"
```

---

## Task 10: Config + wireApp

**Files:**
- Create: `internal/config/config.go`, `internal/config/config_test.go`, `internal/app/app.go`

**Interfaces:**
- Consumes: all adapters and services built so far
- Produces: `config.Load()` returns `Config`; `app.Wire(cfg) (*App, error)` returns fully wired application.

- [ ] **Step 1: Write the failing test for config**

Create `internal/config/config_test.go`:

```go
package config

import (
    "os"
    "path/filepath"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
)

func TestLoad_DefaultsWhenNoFile(t *testing.T) {
    cfg, err := Load(filepath.Join(t.TempDir(), "nonexistent.yaml"))
    require.NoError(t, err)
    assert.Equal(t, "default", cfg.User.ID)
    assert.NotEmpty(t, cfg.DataDir)
    assert.NotEmpty(t, cfg.DBPath)
}

func TestLoad_ReadsYAML(t *testing.T) {
    dir := t.TempDir()
    yamlPath := filepath.Join(dir, "config.yaml")
    err := os.WriteFile(yamlPath, []byte(`
user:
  id: alice
data_dir: /tmp/custom-data
`), 0o644)
    require.NoError(t, err)

    cfg, err := Load(yamlPath)
    require.NoError(t, err)
    assert.Equal(t, "alice", cfg.User.ID)
    assert.Equal(t, "/tmp/custom-data", cfg.DataDir)
}

func TestConfig_DBPathDerivedFromDataDir(t *testing.T) {
    cfg := Config{DataDir: "/some/path"}
    assert.Equal(t, "/some/path/unictx.db", cfg.DBPath())
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
go test ./internal/config/...
```

Expected: FAIL — package undefined.

- [ ] **Step 3: Write config.go**

Create `internal/config/config.go`:

```go
package config

import (
    "os"
    "path/filepath"
    "strconv"

    "gopkg.in/yaml.v3"
)

type Config struct {
    User    UserConfig `yaml:"user"`
    DataDir string     `yaml:"data_dir"`
}

type UserConfig struct {
    ID string `yaml:"id"`
}

// Load reads config from path (if it exists) and applies defaults.
// Missing file is not an error.
func Load(path string) (*Config, error) {
    cfg := &Config{
        User:    UserConfig{ID: "default"},
        DataDir: defaultDataDir(),
    }
    if data, err := os.ReadFile(path); err == nil {
        if err := yaml.Unmarshal(data, cfg); err != nil {
            return nil, err
        }
    }
    if cfg.DataDir == "" {
        cfg.DataDir = defaultDataDir()
    }
    if cfg.User.ID == "" {
        cfg.User.ID = "default"
    }
    return cfg, nil
}

func (c *Config) DBPath() string {
    return filepath.Join(c.DataDir, "unictx.db")
}

func (c *Config) FileStoreDir() string {
    return filepath.Join(c.DataDir, "filestore")
}

// ConfigDir returns the user config dir (XDG-aware, falls back to ~/.config).
func DefaultConfigDir() string {
    if x := os.Getenv("XDG_CONFIG_HOME"); x != "" {
        return filepath.Join(x, "unictx")
    }
    home, err := os.UserHomeDir()
    if err != nil {
        home = "."
    }
    return filepath.Join(home, ".config", "unictx")
}

func defaultDataDir() string {
    if x := os.Getenv("XDG_DATA_HOME"); x != "" {
        return filepath.Join(x, "unictx")
    }
    home, err := os.UserHomeDir()
    if err != nil {
        home = "."
    }
    return filepath.Join(home, ".local", "share", "unictx")
}

// Helper to silence imports if needed for compile-only checks.
var _ = strconv.Itoa
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
go test -race ./internal/config/...
```

Expected: PASS.

- [ ] **Step 5: Write wireApp**

Create `internal/app/app.go`:

```go
package app

import (
    "database/sql"
    "fmt"

    "uni-context/internal/adapter/fsstore"
    "uni-context/internal/adapter/sqlite"
    "uni-context/internal/config"
    "uni-context/internal/port"
    "uni-context/internal/service"
)

type App struct {
    Config  *config.Config
    DB      *sql.DB
    Repo    port.ContextRepo
    Project port.ProjectRepo
    Searcher port.Searcher
    FS      port.FileStore

    Ingest *service.IngestService
    Search *service.SearchService
}

// Wire opens the DB (running migrations), builds adapters and services,
// and returns a fully assembled App. Caller is responsible for App.DB.Close().
func Wire(cfg *config.Config) (*App, error) {
    if err := mkdirp(cfg.DataDir, cfg.FileStoreDir()); err != nil {
        return nil, err
    }
    db, err := sqlite.Open(cfg.DBPath())
    if err != nil {
        return nil, fmt.Errorf("open db: %w", err)
    }
    fs, err := fsstore.New(cfg.FileStoreDir())
    if err != nil {
        _ = db.Close()
        return nil, fmt.Errorf("open filestore: %w", err)
    }
    repo := sqlite.NewContextRepo(db)
    proj := sqlite.NewProjectRepo(db)
    searcher := sqlite.NewSearcher(db)

    return &App{
        Config:   cfg,
        DB:       db,
        Repo:     repo,
        Project:  proj,
        Searcher: searcher,
        FS:       fs,
        Ingest:   service.NewIngestService(repo, fs),
        Search:   service.NewSearchService(searcher, repo),
    }, nil
}

func (a *App) Close() error {
    if a.DB != nil {
        return a.DB.Close()
    }
    return nil
}

func mkdirp(dirs ...string) error {
    for _, d := range dirs {
        if err := osMkdirAll(d); err != nil {
            return fmt.Errorf("mkdir %s: %w", d, err)
        }
    }
    return nil
}
```

Create `internal/app/os.go`:

```go
package app

import "os"

func osMkdirAll(p string) error {
    return os.MkdirAll(p, 0o755)
}
```

- [ ] **Step 6: Verify it builds**

```bash
go build ./...
```

Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add internal/config/ internal/app/
git commit -m "feat(app): add config loader and wireApp"
```

---

## Task 11: CLI Scaffolding (root, config, doctor)

**Files:**
- Create: `internal/cli/root.go`, `internal/cli/config_cmd.go`, `internal/cli/doctor.go`
- Modify: `cmd/unictx/main.go`

**Interfaces:**
- Consumes: `app.Wire`, `config.Load`
- Produces: `cli.Execute(args) error` plus root/config/doctor subcommands.

- [ ] **Step 1: Write root.go**

Create `internal/cli/root.go`:

```go
package cli

import (
    "fmt"
    "os"
    "path/filepath"

    "github.com/spf13/cobra"
    "uni-context/internal/app"
    "uni-context/internal/config"
)

var (
    flagConfigPath string
    flagJSON       bool
    flagVerbose    bool
)

var rootCmd = &cobra.Command{
    Use:   "unictx",
    Short: "Unified context knowledge management",
    PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
        if flagVerbose {
            // Future: configure slog to debug level
        }
        return nil
    },
}

func init() {
    rootCmd.PersistentFlags().StringVar(&flagConfigPath, "config",
        filepath.Join(config.DefaultConfigDir(), "config.yaml"),
        "path to config file")
    rootCmd.PersistentFlags().BoolVar(&flagJSON, "json", false, "output as JSON")
    rootCmd.PersistentFlags().BoolVar(&flagVerbose, "verbose", false, "verbose logging")
}

// Execute runs the root command.
func Execute() error {
    return rootCmd.Execute()
}

// loadApp is a helper for subcommands.
func loadApp() (*app.App, *config.Config, error) {
    cfg, err := config.Load(flagConfigPath)
    if err != nil {
        return nil, nil, fmt.Errorf("load config: %w", err)
    }
    a, err := app.Wire(cfg)
    if err != nil {
        return nil, cfg, err
    }
    return a, cfg, nil
}

// exitCode unwraps known errors and returns the CLI exit code.
func exitCode(err error) int {
    if err == nil {
        return 0
    }
    // simple classification for now
    return 1
}

func die(err error) {
    if err == nil {
        return
    }
    fmt.Fprintln(os.Stderr, "error:", err)
    os.Exit(exitCode(err))
}
```

- [ ] **Step 2: Write config_cmd.go**

Create `internal/cli/config_cmd.go`:

```go
package cli

import (
    "fmt"

    "github.com/spf13/cobra"
    "uni-context/internal/config"
)

var configCmd = &cobra.Command{
    Use:   "config",
    Short: "Inspect uni-context configuration",
}

var configPathCmd = &cobra.Command{
    Use:   "path",
    Short: "Print the config file path",
    RunE: func(cmd *cobra.Command, args []string) error {
        fmt.Println(flagConfigPath)
        return nil
    },
}

var configGetCmd = &cobra.Command{
    Use:   "get <key>",
    Short: "Get a config value (data_dir, db_path, filestore_dir)",
    Args:  cobra.ExactArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        cfg, err := config.Load(flagConfigPath)
        if err != nil {
            return err
        }
        switch args[0] {
        case "data_dir":
            fmt.Println(cfg.DataDir)
        case "db_path":
            fmt.Println(cfg.DBPath())
        case "filestore_dir":
            fmt.Println(cfg.FileStoreDir())
        case "user_id":
            fmt.Println(cfg.User.ID)
        default:
            return fmt.Errorf("unknown key %q (valid: data_dir, db_path, filestore_dir, user_id)", args[0])
        }
        return nil
    },
}

func init() {
    configCmd.AddCommand(configPathCmd, configGetCmd)
    rootCmd.AddCommand(configCmd)
}
```

- [ ] **Step 3: Write doctor.go**

Create `internal/cli/doctor.go`:

```go
package cli

import (
    "fmt"

    "github.com/spf13/cobra"
)

var doctorCmd = &cobra.Command{
    Use:   "doctor",
    Short: "Check that uni-context is set up correctly",
    RunE: func(cmd *cobra.Command, args []string) error {
        a, cfg, err := loadApp()
        if err != nil {
            return fmt.Errorf("setup error: %w", err)
        }
        defer a.DB.Close()

        fmt.Printf("config path:    %s\n", flagConfigPath)
        fmt.Printf("data dir:       %s\n", cfg.DataDir)
        fmt.Printf("db path:        %s\n", cfg.DBPath())
        fmt.Printf("filestore dir:  %s\n", cfg.FileStoreDir())
        fmt.Printf("user id:        %s\n", cfg.User.ID)

        var version string
        if err := a.DB.QueryRow(`SELECT value FROM schema_meta WHERE key='schema_version'`).Scan(&version); err != nil {
            return fmt.Errorf("read schema version: %w", err)
        }
        fmt.Printf("schema version: %s\n", version)
        fmt.Println("status:         OK")
        return nil
    },
}

func init() {
    rootCmd.AddCommand(doctorCmd)
}
```

- [ ] **Step 4: Update main.go**

Replace `cmd/unictx/main.go`:

```go
package main

import (
    "fmt"
    "os"

    "uni-context/internal/cli"
)

var version = "dev"

func main() {
    cli.SetVersion(version)
    if err := cli.Execute(); err != nil {
        fmt.Fprintln(os.Stderr, "error:", err)
        os.Exit(1)
    }
}
```

We need a `SetVersion` helper — add to `internal/cli/root.go`:

```go
// SetVersion records the build version (called from main).
func SetVersion(v string) {
    rootCmd.Version = v
}
```

Append this snippet to `internal/cli/root.go` (after the init() block).

- [ ] **Step 5: Build and smoke-test**

```bash
CGO_ENABLED=1 go build -o /tmp/unictx ./cmd/unictx
/tmp/unictx --help
/tmp/unictx config path
/tmp/unictx doctor
```

Expected: doctor prints paths + `status: OK`.

- [ ] **Step 6: Commit**

```bash
git add internal/cli/ cmd/unictx/main.go
git commit -m "feat(cli): add root, config, and doctor commands"
```

---

## Task 12: CLI user note commands

**Files:**
- Create: `internal/cli/user_note.go`, `internal/cli/output.go`

**Interfaces:**
- Consumes: `service.IngestService`, `service.SearchService`, `port.ContextRepo`
- Produces: `unictx user note add|list|get|delete` subcommands with `--json` output.

- [ ] **Step 1: Write output.go (JSON helpers)**

Create `internal/cli/output.go`:

```go
package cli

import (
    "encoding/json"
    "fmt"
    "os"
)

func printJSON(v any) {
    enc := json.NewEncoder(os.Stdout)
    enc.SetIndent("", "  ")
    if err := enc.Encode(v); err != nil {
        fmt.Fprintln(os.Stderr, "json encode:", err)
        os.Exit(1)
    }
}

type noteOut struct {
    ID        string   `json:"id"`
    Title     string   `json:"title"`
    Summary   string   `json:"summary"`
    Content   string   `json:"content,omitempty"`
    Tags      []string `json:"tags"`
    CreatedAt string   `json:"created_at"`
    UpdatedAt string   `json:"updated_at"`
}

func itemToNoteOut(content string, item any) noteOut {
    // Use type assertion in callers; this is a placeholder shape only.
    return noteOut{}
}
```

Actually that placeholder is unused — let's skip it. Remove the `itemToNoteOut` function and replace with concrete serialization inline in user_note.go.

Rewrite `internal/cli/output.go` (replace the placeholder):

```go
package cli

import (
    "encoding/json"
    "fmt"
    "os"
)

func printJSON(v any) {
    enc := json.NewEncoder(os.Stdout)
    enc.SetIndent("", "  ")
    if err := enc.Encode(v); err != nil {
        fmt.Fprintln(os.Stderr, "json encode:", err)
        os.Exit(1)
    }
}
```

- [ ] **Step 2: Write user_note.go**

Create `internal/cli/user_note.go`:

```go
package cli

import (
    "fmt"
    "io"
    "os"
    ""strings"
    "time"

    "github.com/spf13/cobra"
    "uni-context/internal/domain"
    "uni-context/internal/port"
)

var userCmd = &cobra.Command{
    Use:   "user",
    Short: "Manage personal-scope (user) knowledge",
}

var userNoteCmd = &cobra.Command{
    Use:   "note",
    Short: "Manage personal notes",
}

var (
    noteTitle string
    noteTags  []string
    noteLimit int
)

var userNoteAddCmd = &cobra.Command{
    Use:   "add [content|-]",
    Short: "Add a personal note. Pass - to read content from stdin.",
    Args:  cobra.MaximumNArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        content, err := readContent(args)
        if err != nil {
            return err
        }
        a, cfg, err := loadApp()
        if err != nil {
            return err
        }
        defer a.DB.Close()

        id, err := a.Ingest.Create(cmd.Context(), inputFromFlags(
            domain.ScopeUser, domain.KindNote, domain.SourceManual,
            cfg.User.ID, "", noteTitle, content, noteTags,
        ))
        if err != nil {
            return err
        }
        if flagJSON {
            printJSON(map[string]string{"id": id, "status": "added"})
        } else {
            fmt.Printf("added: %s\n", id)
        }
        return nil
    },
}

var userNoteListCmd = &cobra.Command{
    Use:   "list",
    Short: "List personal notes (newest first)",
    RunE: func(cmd *cobra.Command, args []string) error {
        a, cfg, err := loadApp()
        if err != nil {
            return err
        }
        defer a.DB.Close()
        if noteLimit <= 0 {
            noteLimit = 20
        }
        items, _, err := a.Repo.List(cmd.Context(), port.ItemFilter{
            Scopes: []domain.Scope{domain.ScopeUser},
            OwnerUserID: cfg.User.ID,
            Kinds:  []domain.Kind{domain.KindNote},
            Limit:  noteLimit,
        })
        if err != nil {
            return err
        }
        if flagJSON {
            out := make([]map[string]any, 0, len(items))
            for _, it := range items {
                out = append(out, map[string]any{
                    "id":         it.ID,
                    "title":      it.Title,
                    "tags":       it.Tags,
                    "created_at": it.CreatedAt.Format(time.RFC3339),
                })
            }
            printJSON(out)
            return nil
        }
        if len(items) == 0 {
            fmt.Println("(no notes)")
            return nil
        }
        for _, it := range items {
            tags := strings.Join(it.Tags, ",")
            fmt.Printf("%s  %s  [%s]\n", it.ID, it.Title, tags)
        }
        return nil
    },
}

var userNoteGetCmd = &cobra.Command{
    Use:   "get <id>",
    Short: "Show a single note",
    Args:  cobra.ExactArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        a, _, err := loadApp()
        if err != nil {
            return err
        }
        defer a.DB.Close()
        item, err := a.Repo.Get(cmd.Context(), args[0])
        if err != nil {
            return err
        }
        content := item.Content
        if content == "" && item.ContentURI != "" {
            data, err := a.FS.Get(item.ContentURI)
            if err != nil {
                return fmt.Errorf("load external content: %w", err)
            }
            content = string(data)
        }
        if flagJSON {
            printJSON(map[string]any{
                "id":         item.ID,
                "title":      item.Title,
                "summary":    item.Summary,
                "content":    content,
                "tags":       item.Tags,
                "created_at": item.CreatedAt.Format(time.RFC3339),
                "updated_at": item.UpdatedAt.Format(time.RFC3339),
            })
            return nil
        }
        fmt.Printf("id:    %s\n", item.ID)
        fmt.Printf("title: %s\n", item.Title)
        fmt.Printf("tags:  %s\n", strings.Join(item.Tags, ", "))
        fmt.Println("---")
        fmt.Println(content)
        return nil
    },
}

var userNoteDeleteCmd = &cobra.Command{
    Use:   "delete <id>",
    Short: "Delete a note",
    Args:  cobra.ExactArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        a, _, err := loadApp()
        if err != nil {
            return err
        }
        defer a.DB.Close()
        if err := a.Repo.Delete(cmd.Context(), args[0]); err != nil {
            return err
        }
        if flagJSON {
            printJSON(map[string]string{"id": args[0], "status": "deleted"})
        } else {
            fmt.Printf("deleted: %s\n", args[0])
        }
        return nil
    },
}

func init() {
    userNoteAddCmd.Flags().StringVar(&noteTitle, "title", "", "note title")
    userNoteAddCmd.Flags().StringSliceVar(&noteTags, "tag", nil, "tags (comma-separated or repeat)")
    userNoteListCmd.Flags().IntVar(&noteLimit, "limit", 20, "max items to return")

    userNoteCmd.AddCommand(userNoteAddCmd, userNoteListCmd, userNoteGetCmd, userNoteDeleteCmd)
    userCmd.AddCommand(userNoteCmd)
    rootCmd.AddCommand(userCmd)
}

func readContent(args []string) (string, error) {
    if len(args) == 0 || args[0] != "-" {
        if len(args) == 0 {
            return "", fmt.Errorf("content required (positional arg or - for stdin)")
        }
        return args[0], nil
    }
    data, err := io.ReadAll(os.Stdin)
    if err != nil {
        return "", fmt.Errorf("read stdin: %w", err)
    }
    return string(data), nil
}

func inputFromFlags(scope domain.Scope, kind domain.Kind, source domain.Source,
    owner, project, title, content string, tags []string,
) serviceInput {
    return serviceInput{
        Scope: scope, Kind: kind, Source: source,
        OwnerUserID: owner, ProjectID: project,
        Title: title, Content: content, Tags: tags,
    }
}

// serviceInput mirrors service.Input; we wrap to avoid import cycle in flags.
type serviceInput struct {
    Scope       domain.Scope
    Kind        domain.Kind
    Source      domain.Source
    OwnerUserID string
    ProjectID   string
    Title       string
    Content     string
    Tags        []string
}
```

Hmm, this `serviceInput` indirection is awkward. Let me simplify by directly importing `service.Input`:

Replace the last 22 lines of `user_note.go` (the `inputFromFlags` and `serviceInput` block) with:

```go
func inputFromFlags(scope domain.Scope, kind domain.Kind, source domain.Source,
    owner, project, title, content string, tags []string,
) service.Input {
    return service.Input{
        Scope: scope, Kind: kind, Source: source,
        OwnerUserID: owner, ProjectID: project,
        Title: title, Content: content, Tags: tags,
    }
}
```

And add the import to user_note.go imports:

```go
import (
    // ...existing...
    "uni-context/internal/service"
)
```

- [ ] **Step 3: Build to verify it compiles**

```bash
CGO_ENABLED=1 go build ./...
```

Expected: no errors.

- [ ] **Step 4: Smoke test**

```bash
CGO_ENABLED=1 go build -o /tmp/unictx ./cmd/unictx
export HOME=/tmp/unictx-test
mkdir -p $HOME

/tmp/unictx user note add "first note" --title "Hello" --tag test --tag demo
/tmp/unictx user note list
/tmp/unictx user note list --json
/tmp/unictx user note get <id-from-list>
/tmp/unictx user note delete <id>

# Test stdin
echo "from stdin" | /tmp/unictx user note add - --title "Stdin Note"
```

Expected: add/list/get/delete all work; `--json` outputs valid JSON.

- [ ] **Step 5: Commit**

```bash
git add internal/cli/output.go internal/cli/user_note.go
git commit -m "feat(cli): add 'user note' add/list/get/delete commands"
```

---

## Task 13: CLI search command

**Files:**
- Create: `internal/cli/search.go`

**Interfaces:**
- Consumes: `service.SearchService`
- Produces: `unictx search <query>` with `--scope`, `--kind`, `--limit`, `--mode` (only `fts-only` is implemented in this plan), `--json` flags.

- [ ] **Step 1: Write search.go**

Create `internal/cli/search.go`:

```go
package cli

import (
    "fmt"
    "strings"
    "time"

    "github.com/spf13/cobra"
    "uni-context/internal/domain"
)

var (
    searchScopes []string
    searchKinds  []string
    searchLimit  int
    searchMode   string
)

var searchCmd = &cobra.Command{
    Use:   "search <query>",
    Short: "Search across all scopes (Plan 1: FTS-only; vector mode arrives in Plan 2)",
    Args:  cobra.MinimumNArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        query := strings.Join(args, " ")
        mode := searchMode
        if mode == "" {
            mode = "fts-only" // Plan 1 default; will become "hybrid" in Plan 2
        }
        if mode != "fts-only" {
            return fmt.Errorf("--mode %q not supported in Plan 1 (only 'fts-only')", mode)
        }

        a, _, err := loadApp()
        if err != nil {
            return err
        }
        defer a.DB.Close()

        if searchLimit <= 0 {
            searchLimit = 20
        }

        resp, err := a.Search.Search(cmd.Context(), serviceSearchReq(query))
        if err != nil {
            return err
        }

        if flagJSON {
            out := make([]map[string]any, 0, len(resp.Results))
            for _, r := range resp.Results {
                out = append(out, map[string]any{
                    "id":         r.Item.ID,
                    "title":      r.Item.Title,
                    "scope":      string(r.Item.Scope),
                    "kind":       string(r.Item.Kind),
                    "score":      r.Score,
                    "snippet":    r.Snippet,
                    "tags":       r.Item.Tags,
                    "created_at": r.Item.CreatedAt.Format(time.RFC3339),
                })
            }
            printJSON(map[string]any{
                "results": out,
                "total":   resp.Total,
                "mode":    mode,
            })
            return nil
        }

        if len(resp.Results) == 0 {
            fmt.Println("(no matches)")
            return nil
        }
        for _, r := range resp.Results {
            fmt.Printf("[%s]  %s\n  scope=%s kind=%s score=%.3f\n  %s\n\n",
                r.Item.ID[:8], r.Item.Title,
                r.Item.Scope, r.Item.Kind, r.Score, r.Snippet)
        }
        return nil
    },
}

func serviceSearchReq(query string) service.SearchRequest {
    return service.SearchRequest{
        Query:  query,
        Scopes: parseScopes(searchScopes),
        Kinds:  parseKinds(searchKinds),
        Limit:  searchLimit,
    }
}

func parseScopes(in []string) []domain.Scope {
    out := make([]domain.Scope, 0, len(in))
    for _, s := range in {
        out = append(out, domain.Scope(s))
    }
    return out
}

func parseKinds(in []string) []domain.Kind {
    out := make([]domain.Kind, 0, len(in))
    for _, s := range in {
        out = append(out, domain.Kind(s))
    }
    return out
}

func init() {
    searchCmd.Flags().StringSliceVar(&searchScopes, "scope", nil, "filter by scope (user,project,global)")
    searchCmd.Flags().StringSliceVar(&searchKinds, "kind", nil, "filter by kind (note,doc,memory,...)")
    searchCmd.Flags().IntVar(&searchLimit, "limit", 20, "max results")
    searchCmd.Flags().StringVar(&searchMode, "mode", "fts-only", "search mode (Plan 1: fts-only)")
    rootCmd.AddCommand(searchCmd)
}
```

You'll also need to import `service` in search.go. Update imports:

```go
import (
    "fmt"
    "strings"
    "time"

    "github.com/spf13/cobra"
    "uni-context/internal/domain"
    "uni-context/internal/service"
)
```

- [ ] **Step 2: Build and smoke test**

```bash
CGO_ENABLED=1 go build -o /tmp/unictx ./cmd/unictx

export HOME=/tmp/unictx-test

/tmp/unictx user note add "How to deploy Go services to kubernetes" --title "Deploy guide"
/tmp/unictx user note add "Python web scraping with requests" --title "Scraping"

/tmp/unictx search "deploy"
/tmp/unictx search "deploy" --json
/tmp/unictx search "scraping" --scope user
/tmp/unictx search "nonexistent"
/tmp/unictx search "deploy" --mode hybrid  # should error: not supported in Plan 1
```

Expected: search returns matching notes; nonexistent returns `(no matches)`; hybrid mode returns error.

- [ ] **Step 3: Commit**

```bash
git add internal/cli/search.go
git commit -m "feat(cli): add 'search' command (FTS-only mode for Plan 1)"
```

---

## Task 14: E2E Test + CI

**Files:**
- Create: `internal/cli/e2e_test.go`, `.github/workflows/test.yml`

**Interfaces:**
- Produces: end-to-end test that exercises the full CLI; GitHub Actions workflow that runs all tests on every push.

- [ ] **Step 1: Write E2E test**

Create `internal/cli/e2e_test.go`:

```go
//go:build e2e

package cli

import (
    "bytes"
    "encoding/json"
    "os"
    "os/exec"
    "path/filepath"
    "runtime"
    "strings"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
)

// binPath returns the path to the built unictx binary.
// The CI workflow builds it before running tests; for local runs you can
// `make build` first.
func binPath(t *testing.T) string {
    t.Helper()
    candidates := []string{
        os.Getenv("UNICTX_BIN"),
        "../../unictx",
        "../../dist/unictx",
    }
    for _, c := range candidates {
        if c == "" {
            continue
        }
        if _, err := os.Stat(c); err == nil {
            return c
        }
    }
    t.Skip("unictx binary not built; run `make build` first")
    return ""
}

func run(t *testing.T, home string, args ...string) (string, int) {
    t.Helper()
    cmd := exec.Command(binPath(t), args...)
    cmd.Env = append(os.Environ(), "HOME="+home, "XDG_DATA_HOME="+filepath.Join(home, ".local", "share"))
    var out, errBuf bytes.Buffer
    cmd.Stdout = &out
    cmd.Stderr = &errBuf
    err := cmd.Run()
    exitCode := 0
    if err != nil {
        if ee, ok := err.(*exec.ExitError); ok {
            exitCode = ee.ExitCode()
        } else {
            t.Fatalf("spawn binary: %v", err)
        }
    }
    if exitCode != 0 && testing.Verbose() {
        t.Logf("stderr: %s", errBuf.String())
    }
    return out.String(), exitCode
}

func TestE2E_NoteLifecycleAndSearch(t *testing.T) {
    home := t.TempDir()

    // Sanity: doctor works on fresh state
    _, code := run(t, home, "doctor")
    require.Zero(t, code, "doctor should succeed on fresh state")

    // Add note A
    outA, code := run(t, home, "user", "note", "add", "How to deploy Go services",
        "--title", "Deploy Guide", "--tag", "go", "--tag", "deploy", "--json")
    require.Zero(t, code)
    var respA struct {
        ID string `json:"id"`
    }
    require.NoError(t, json.Unmarshal([]byte(outA), &respA))
    require.NotEmpty(t, respA.ID)

    // Add note B
    _, code = run(t, home, "user", "note", "add", "Python scraping tutorial",
        "--title", "Scraping", "--tag", "python")
    require.Zero(t, code)

    // List should show 2
    outList, code := run(t, home, "user", "note", "list", "--json")
    require.Zero(t, code)
    var listResp []map[string]any
    require.NoError(t, json.Unmarshal([]byte(outList), &listResp))
    assert.Len(t, listResp, 2)

    // Search "deploy" should find note A
    outSearch, code := run(t, home, "search", "deploy", "--json")
    require.Zero(t, code)
    var searchResp struct {
        Results []map[string]any `json:"results"`
        Total   int              `json:"total"`
    }
    require.NoError(t, json.Unmarshal([]byte(outSearch), &searchResp))
    assert.GreaterOrEqual(t, searchResp.Total, 1)
    assert.Equal(t, respA.ID, searchResp.Results[0]["id"])

    // Get the note
    outGet, code := run(t, home, "user", "note", "get", respA.ID, "--json")
    require.Zero(t, code)
    var getResp map[string]any
    require.NoError(t, json.Unmarshal([]byte(outGet), &getResp))
    assert.Equal(t, "Deploy Guide", getResp["title"])

    // Delete
    _, code = run(t, home, "user", "note", "delete", respA.ID, "--json")
    require.Zero(t, code)

    // Search should now miss it
    outSearch2, _ := run(t, home, "search", "deploy", "--json")
    var searchResp2 struct{ Total int `json:"total"` }
    _ = json.Unmarshal([]byte(outSearch2), &searchResp2)
    assert.Equal(t, 0, searchResp2.Total, "deleted note should not match")
}

func TestE2E_LargeContentExternalized(t *testing.T) {
    home := t.TempDir()
    big := strings.Repeat("long content word ", 500) // ~9KB

    // Pass via stdin
    cmd := exec.Command(binPath(t), "user", "note", "add", "-", "--title", "Big")
    cmd.Env = append(os.Environ(), "HOME="+home,
        "XDG_DATA_HOME="+filepath.Join(home, ".local", "share"))
    cmd.Stdin = strings.NewReader(big)
    var out bytes.Buffer
    cmd.Stdout = &out
    require.NoError(t, cmd.Run())

    var resp struct{ ID string `json:"id"` }
    require.NoError(t, json.Unmarshal(out.Bytes(), &resp))

    // get should return full content
    outGet, code := run(t, home, "user", "note", "get", resp.ID, "--json")
    require.Zero(t, code)
    var getResp map[string]any
    require.NoError(t, json.Unmarshal([]byte(outGet), &getResp))
    assert.Equal(t, big, getResp["content"])
}

// silence unused warnings when cross-compiling
var _ = runtime.GOOS
```

- [ ] **Step 2: Build binary and run E2E**

```bash
CGO_ENABLED=1 go build -o unictx ./cmd/unictx
CGO_ENABLED=1 go test -tags=e2e -v ./internal/cli/...
```

Expected: both E2E tests PASS.

- [ ] **Step 3: Write GitHub Actions workflow**

Create `.github/workflows/test.yml`:

```yaml
name: test

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: '1.22'
      - name: Build binary (for e2e tests)
        run: CGO_ENABLED=1 go build -o unictx ./cmd/unictx
      - name: Run unit + integration tests
        run: CGO_ENABLED=1 go test -race -timeout 5m ./...
      - name: Run E2E tests
        env:
          UNICTX_BIN: ${{ github.workspace }}/unictx
        run: CGO_ENABLED=1 go test -tags=e2e -timeout 5m ./internal/cli/...
```

- [ ] **Step 4: Run full local test suite**

```bash
CGO_ENABLED=1 go test -race ./...
CGO_ENABLED=1 go test -race -tags=e2e ./internal/cli/...
```

Expected: all PASS.

- [ ] **Step 5: Update .gitignore to exclude the locally-built binary**

Edit `.gitignore` to ensure `/unictx` (project root binary) is excluded — it already is from Task 1.

- [ ] **Step 6: Commit**

```bash
git add internal/cli/e2e_test.go .github/workflows/test.yml
git commit -m "test: add end-to-end CLI tests and GitHub Actions workflow"
```

---

## Self-Review

### Spec coverage

| Spec section | Plan 1 task(s) | Notes |
|---|---|---|
| §1.3 decision 1 (three scopes equal) | Task 2 | Scope type + invariants; full coverage in Plan 1 |
| §1.3 decision 2 (local-first) | Task 10 | Default data dir resolves to `~/.local/share/unictx/` |
| §1.3 decision 3 (Go) | Task 1 | go.mod |
| §1.3 decision 6 (hybrid search) | Task 6, 9, 13 | **FTS-only mode in Plan 1**; hybrid arrives Plan 2 |
| §1.3 decision 9 (cgo) | Task 1, 4 | mattn/go-sqlite3 |
| §1.3 decision 12 (MCP update_note) | — | Plan 6 |
| §2.2 Hexagonal architecture | All tasks | domain/port/adapter/service split |
| §2.3 Go package structure | Task 1 | cmd/internal/pkg with the planned subdirectories |
| §3.1 context_item table | Task 4 | Full schema in 0001_init.sql |
| §3.2 FTS5 + trigram | Task 4, 6 | trigram tokenizer; FTS triggers |
| §3.5 project table | Task 4 | Forward-compat; ProjectRepo in Task 5 |
| §3.7 schema_meta | Task 4 | |
| §3.9 SQLite PRAGMAs | Task 4 | All five in DSN |
| §3.10 design decisions | Task 2, 8 | 4KB threshold (ContentInlineLimit), UUID v7 |
| §4.1 CLI user note | Task 12 | add/list/get/delete with --json |
| §4.1 CLI search | Task 13 | --mode fts-only for Plan 1 |
| §4.1 CLI config/doctor | Task 11 | |
| §5.1 ingest pipeline | Task 8 | validate → normalize → externalize → insert |
| §5.2 search (partial) | Task 9 | FTS path only; RRF comes Plan 2 |
| §6.1 transactions | Task 5 | Per-op transaction in repo |
| §6.4 schema migrations | Task 4 | Embedded versioned SQL |
| §6.7 error codes | Task 11 | Domain errors; full exit code mapping comes later |
| §7 testing strategy | All tasks | TDD; in-memory SQLite; race detector |

### Placeholder scan

Searched the plan for `TBD`, `TODO`, `FIXME`, `XXX`, "implement later", "add appropriate error handling", "similar to Task N". The search fixture in Task 9 had a tangled `spyRepo` first draft; I replaced it with the simpler real-adapter version. All other steps contain concrete code.

### Type consistency

- `domain.ContextItem`, `domain.Scope`, `domain.Kind`, `domain.Source` — used consistently across Tasks 2, 5, 8, 9, 12, 13.
- `port.ContextRepo`, `port.ProjectRepo`, `port.FileStore`, `port.Searcher` — used consistently across Tasks 3, 5, 7, 8, 9.
- `service.IngestService`, `service.SearchService` — created in Tasks 8/9, used in Tasks 10/12/13.
- `app.App` fields (`Ingest`, `Search`, `Repo`, `FS`, `DB`) — used in Tasks 11/12/13.
- `sqlite.Open`, `sqlite.Migrate`, `sqlite.NewContextRepo`, `sqlite.NewProjectRepo`, `sqlite.NewSearcher` — used in Tasks 4/5/6/9/10.

### Known scope/ambiguity notes

1. **`context_item.any_embedding` field exists in Plan 1 but is unused (always 0)**. This is intentional — the schema is forward-compatible with Plan 2's embedding pipeline. No code references it beyond persistence.
2. **`relation`, `agent`, `conversation`, `sync_state`, `embedding_model`, `context_embedding`, `embed_queue` tables are NOT in Plan 1's 0001_init.sql**. They arrive in later plans via subsequent migrations.
3. **`--mode hybrid` is rejected in Plan 1's `search` command**. This is a deliberate Plan 1 boundary, not a placeholder.
4. **`fsstore` uses a real file backend; no in-memory fake**. Service tests in Tasks 8/9 use real fsstore with `t.TempDir()` for honesty. Adapter test in Task 7 uses real fsstore directly.
5. **`fakeRepo` in Task 8 is hand-rolled** in `internal/service/fake_repo_test.go` rather than generated. It's small enough that maintenance is trivial. Task 9 switched to real adapters for the search service test (more honest), so `fakeRepo` is only used by ingest tests.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-19-foundation.md`.

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Each task's deliverable is independently verifiable.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
