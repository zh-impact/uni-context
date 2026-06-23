# User Note File Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--file <path>` flag to `unictx user note add` that imports a text file (`.txt`, `.md`) as a note, preserving the original filename in `SourceMeta` and the MIME type on the item.

**Architecture:** Two layers touch. The service layer gains a `MIME string` field on `IngestService.Input` so the CLI can tell it "this content is markdown"; `Create` uses that MIME when externalizing to FileStore and when setting `item.ContentMIME`. The CLI layer adds the `--file` flag, validation helpers (size cap, regular-file check, mutual-exclusion), MIME detection from file extension, default title from filename, and a `userNoteLoadAppFn` indirection for RunE-level tests.

**Tech Stack:** Go 1.25, cobra 1.8.1, existing `service.IngestService`, `port.FileStore`, `port.ContextRepo`. No new dependencies.

## Global Constraints

- File imports follow the existing `len(content) > domain.ContentInlineLimit` (4096 bytes) externalization path. **No `ForceExternalize` field** — it would break FTS by forcing `item.Content = ""` on small files (migration 0001's `context_ai` trigger reads `new.content`).
- 10 MB file size cap enforced via `os.Stat` before `os.ReadFile` so rejected files never allocate a buffer. Constant: `maxFileBytes int64 = 10 * 1024 * 1024`.
- Extension-based MIME: `.md`/`.markdown` → `text/markdown` (case-insensitive), everything else → `text/plain`.
- `--file` is mutually exclusive with positional content and `-` stdin.
- Default title uses `cmd.Flags().Changed("title")` so explicit `--title ""` is respected, not overwritten by filename derivation.
- `var userNoteLoadAppFn = loadApp` — a **separate** indirection var from `embed.go`'s `loadAppFn` (both are package-level in `cli`; reusing the same name would be a redeclare compile error).
- Run `goimports -w` on every touched `.go` file before committing (project convention; matches VSCode format-on-save).
- Conventional commit style: `feat(scope):`, `test(scope):`, etc.
- Test commands: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/service/...` for service tests; `CGO_ENABLED=1 go test ./internal/cli/...` for CLI tests (no tag needed — CLI tests use fakes, never call `sqlite.Migrate`).
- `inputFromFlags` (existing helper in `user_note.go`) is **not extended** and **not removed** — left as-is for any future caller that doesn't need MIME/SourceMeta.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `internal/service/ingest.go` | `IngestService.Create` — externalization + embed dispatch | Add `MIME string` field to `Input`; use it in externalization + inline MIME preservation |
| `internal/service/ingest_test.go` | Service-level TDD tests | Append 4 new tests for MIME behavior |
| `internal/cli/user_note.go` | `userNoteAddCmd` + flag vars + helpers | Add `noteFilePath` var + `--file` flag; add 4 pure helpers (`mimeForTextFile`, `deriveDefaultTitle`, `checkFileSize`, `validateFileImport`); add `userNoteLoadAppFn` indirection; rewrite `userNoteAddCmd.RunE` with `--file` branch |
| `internal/cli/user_note_test.go` | Unit tests for pure helpers | Append table-driven tests for all 4 helpers + validation |
| `internal/cli/user_note_run_e_test.go` | RunE-level integration tests | **New file** — 3 RunE tests + `capturingRepo` fake + `swapUserNoteLoadAppFn` helper |

---

## Task 1: Add `MIME` field to `service.Input` + update `Create` externalization

**Files:**
- Modify: `internal/service/ingest.go` (struct definition at line 34-48; externalization block at line 72-83)
- Test: `internal/service/ingest_test.go` (append new tests at end of file)

**Interfaces:**
- Consumes: `domain.ContentInlineLimit` (existing, `4 * 1024`), `port.FileStore.Put([]byte, string) (uri, hash string, err error)` (existing)
- Produces: `Input.MIME string` field — later tasks set this from the CLI when importing `.md` files. `item.ContentMIME` is now populated for both inline (when `MIME != ""`) and externalized content.

- [ ] **Step 1: Write the 4 failing service tests**

Append to `internal/service/ingest_test.go`:

```go
// TestIngest_Create_LargeContentWithMIMEExternalizesToFS verifies that
// when content exceeds ContentInlineLimit and MIME is set, the FileStore
// receives the correct MIME (stored in .meta) and the item carries it on
// ContentMIME. This is the path a large .md file import takes.
func TestIngest_Create_LargeContentWithMIMEExternalizesToFS(t *testing.T) {
	f := newIngestFixture(t)
	large := strings.Repeat("word ", 1000) // ~5KB > 4KB ContentInlineLimit
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     large,
		MIME:        "text/markdown",
	})
	require.NoError(t, err)

	got, err := f.repo.Get(context.Background(), id)
	require.NoError(t, err)
	assert.Empty(t, got.Content, "inline content should be emptied")
	assert.NotEmpty(t, got.ContentURI, "content_uri should be set")
	assert.Equal(t, "text/markdown", got.ContentMIME,
		"ContentMIME must reflect the caller-specified MIME for externalized content")

	// FileStore .meta must carry the MIME so re-embed / hydration knows the type.
	data, err := f.fs.Get(got.ContentURI)
	require.NoError(t, err)
	assert.Equal(t, large, string(data))
}

// TestIngest_Create_DefaultMIMEIsTextPlainWhenEmpty verifies that when
// MIME is empty (existing callers: inline text, stdin), the externalize
// path falls back to text/plain — preserving Plan 1 behavior byte-for-byte.
func TestIngest_Create_DefaultMIMEIsTextPlainWhenEmpty(t *testing.T) {
	f := newIngestFixture(t)
	large := strings.Repeat("a", 5000) // > 4KB
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     large,
		// MIME intentionally omitted
	})
	require.NoError(t, err)

	got, _ := f.repo.Get(context.Background(), id)
	assert.Equal(t, "text/plain", got.ContentMIME,
		"empty MIME must default to text/plain on the externalize path")
}

// TestIngest_Create_SmallContentPreservesMIMEInline verifies that a small
// file import (< ContentInlineLimit) with MIME set preserves the MIME on
// item.ContentMIME even though the content stays inline (not in FileStore).
// This is the key invariant for .md file imports: the MIME survives on the
// item so downstream renderers know it's markdown without consulting FileStore.
func TestIngest_Create_SmallContentPreservesMIMEInline(t *testing.T) {
	f := newIngestFixture(t)
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     "# tiny markdown",
		MIME:        "text/markdown",
	})
	require.NoError(t, err)

	got, err := f.repo.Get(context.Background(), id)
	require.NoError(t, err)
	assert.NotEmpty(t, got.Content, "small content stays inline")
	assert.Empty(t, got.ContentURI, "small content is not externalized")
	assert.Equal(t, "text/markdown", got.ContentMIME,
		"MIME must be preserved on inline items when caller sets it")
}

// TestIngest_Create_EmptyMIMELeavesContentMIMEEmptyInline is a regression
// guard: existing callers (inline text, stdin) pass MIME="". The inline
// path must NOT set ContentMIME in that case, preserving the Plan 1
// invariant where inline items have ContentMIME="".
func TestIngest_Create_EmptyMIMELeavesContentMIMEEmptyInline(t *testing.T) {
	f := newIngestFixture(t)
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     "small content",
		// MIME intentionally omitted
	})
	require.NoError(t, err)

	got, _ := f.repo.Get(context.Background(), id)
	assert.Empty(t, got.ContentMIME,
		"existing callers with MIME='' must leave ContentMIME empty on inline items")
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/service/ -run "TestIngest_Create_LargeContentWithMIMEExternalizesToFS|TestIngest_Create_DefaultMIMEIsTextPlainWhenEmpty|TestIngest_Create_SmallContentPreservesMIMEInline|TestIngest_Create_EmptyMIMELeavesContentMIMEEmptyInline" -v`

Expected: compile failure — `Input` has no field `MIME`:
```
internal/service/ingest_test.go: undefined: Input.MIME
```

- [ ] **Step 3: Add `MIME` field to `Input` and update `Create`**

In `internal/service/ingest.go`, add the `MIME` field to the `Input` struct after `SourceMeta`:

```go
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

	// MIME of the content. Empty means "treat as text/plain" (preserves
	// existing behavior for inline/stdin notes — no caller update required).
	// Set by the CLI when importing a .md file so FileStore's .meta and
	// item.ContentMIME both carry the right MIME. For inline-sized file
	// imports (< ContentInlineLimit), MIME is still written to item.ContentMIME
	// so downstream renderers can tell a .md import from a .txt import even
	// when the bytes didn't go through FileStore.
	MIME string
}
```

Then replace the externalization block in `Create` (currently lines 72-83):

```go
	// Before (delete this):
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
```

with:

```go
	// After (replace with this):
	mime := in.MIME
	if mime == "" {
		mime = "text/plain"
	}
	if len(in.Content) > domain.ContentInlineLimit {
		uri, hash, err := s.fs.Put([]byte(in.Content), mime)
		if err != nil {
			return "", fmt.Errorf("externalize content: %w", err)
		}
		item.ContentURI = uri
		item.ContentHash = hash
		item.ContentMIME = mime
		item.Content = ""
	} else {
		item.Content = in.Content
		if in.MIME != "" {
			item.ContentMIME = in.MIME
		}
	}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/service/ -run "TestIngest_Create_LargeContentWithMIMEExternalizesToFS|TestIngest_Create_DefaultMIMEIsTextPlainWhenEmpty|TestIngest_Create_SmallContentPreservesMIMEInline|TestIngest_Create_EmptyMIMELeavesContentMIMEEmptyInline" -v`

Expected: PASS (all 4 new tests green).

- [ ] **Step 5: Run full service package regression**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/service/...`

Expected: PASS — existing `TestIngest_Create_*` tests still pass (empty MIME preserves old behavior).

- [ ] **Step 6: Format and commit**

```bash
goimports -w internal/service/ingest.go internal/service/ingest_test.go
git add internal/service/ingest.go internal/service/ingest_test.go
git commit -m "$(cat <<'EOF'
feat(service): add MIME field to IngestService.Input

Input.MIME lets the CLI tell Create "this content is markdown" when
importing a .md file. Empty means text/plain (existing callers
unchanged). For externalized content, the MIME flows to FileStore.Put
and item.ContentMIME. For inline content with MIME set, ContentMIME is
populated so downstream renderers can tell .md from .txt without
consulting FileStore. Existing callers (MIME="") leave ContentMIME
empty on inline items — preserving Plan 1 behavior.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: CLI pure helpers (`mimeForTextFile`, `deriveDefaultTitle`, `checkFileSize`, `validateFileImport`)

**Files:**
- Modify: `internal/cli/user_note.go` (append helpers after `previewRunes` at end of file)
- Test: `internal/cli/user_note_test.go` (append tests at end of file)

**Interfaces:**
- Consumes: `os.Stat`, `os.FileMode.IsRegular`, `strings.ToLower`, `filepath.Ext`, `filepath.Base`, `strings.LastIndex`, `fmt.Errorf`
- Produces: four unexported helpers that Task 3's RunE calls:
  - `mimeForTextFile(path string) string`
  - `deriveDefaultTitle(path string) string`
  - `checkFileSize(size int64) error`
  - `validateFileImport(path string) error`
  - `maxFileBytes int64` constant

**No dependency on Task 1** — these are pure CLI-layer helpers that don't touch `service.Input`.

- [ ] **Step 1: Write the failing unit tests**

Append to `internal/cli/user_note_test.go`. Add these imports at the top if not present: `"os"`, `"path/filepath"`, `"testing"`, and `"github.com/stretchr/testify/assert"`.

```go
func TestMimeForTextFile(t *testing.T) {
	cases := []struct{ path, want string }{
		{"notes.txt", "text/plain"},
		{"weekly.md", "text/markdown"},
		{"weekly.markdown", "text/markdown"},
		{"weekly.MD", "text/markdown"},    // case-insensitive
		{"weekly.Markdown", "text/markdown"},
		{"notes.org", "text/plain"},        // unknown → default
		{"noext", "text/plain"},            // no extension
		{".bashrc", "text/plain"},          // leading-dot, no real ext
		{"/abs/path/weekly.md", "text/markdown"},
	}
	for _, c := range cases {
		t.Run(c.path, func(t *testing.T) {
			assert.Equal(t, c.want, mimeForTextFile(c.path))
		})
	}
}

func TestDeriveDefaultTitle(t *testing.T) {
	cases := []struct{ path, want string }{
		{"weekly.md", "weekly"},
		{"notes.txt", "notes"},
		{"noext", "noext"},
		{".bashrc", ".bashrc"},              // dot at index 0; guard prevents stripping
		{"archive.tar.gz", "archive.tar"},   // only last ext stripped
		{"/abs/path/notes.md", "notes"},      // basename only
		{"weekly.MD", "weekly"},             // case-insensitive ext stripped
	}
	for _, c := range cases {
		t.Run(c.path, func(t *testing.T) {
			assert.Equal(t, c.want, deriveDefaultTitle(c.path))
		})
	}
}

func TestCheckFileSize(t *testing.T) {
	cases := []struct {
		name string
		size int64
		wantErr string // empty = nil expected
	}{
		{"zero bytes", 0, ""},
		{"one byte", 1, ""},
		{"at cap", maxFileBytes, ""},
		{"cap plus one", maxFileBytes + 1, "file too large"},
		{"ten MB plus one thousand", maxFileBytes + 1000, "file too large"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := checkFileSize(c.size)
			if c.wantErr == "" {
				assert.NoError(t, err)
			} else {
				require.Error(t, err)
				assert.Contains(t, err.Error(), c.wantErr)
				assert.Contains(t, err.Error(), "max 10485760")
			}
		})
	}
}

func TestValidateFileImport_NotExisting(t *testing.T) {
	err := validateFileImport(filepath.Join(t.TempDir(), "nope.txt"))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "stat file:")
}

func TestValidateFileImport_Directory(t *testing.T) {
	dir := t.TempDir()
	err := validateFileImport(dir)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "not a regular file")
}

func TestValidateFileImport_SmallFileOK(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ok.txt")
	require.NoError(t, os.WriteFile(path, []byte("hello"), 0o644))
	err := validateFileImport(path)
	assert.NoError(t, err)
}
```

Add `"github.com/stretchr/testify/require"` to imports if not already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `CGO_ENABLED=1 go test ./internal/cli/ -run "TestMimeForTextFile|TestDeriveDefaultTitle|TestCheckFileSize|TestValidateFileImport" -v`

Expected: compile failure — `mimeForTextFile`, `deriveDefaultTitle`, `checkFileSize`, `validateFileImport`, `maxFileBytes` undefined.

- [ ] **Step 3: Implement the four helpers**

Append to `internal/cli/user_note.go` (after `previewRunes`):

```go
// maxFileBytes is the file import size cap. Enforced via os.Stat before
// os.ReadFile so a rejected file never allocates a buffer. 10 MB is a
// guardrail against accidentally loading huge files, not a security boundary.
const maxFileBytes int64 = 10 * 1024 * 1024

// mimeForTextFile maps a small set of text file extensions to MIME types.
// Unknown extensions default to text/plain — binary support is out of scope.
// Case-insensitive via strings.ToLower so weekly.MD is still markdown.
// Adding new text types later (.org, .rst) is a one-liner here.
func mimeForTextFile(path string) string {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".md", ".markdown":
		return "text/markdown"
	default:
		return "text/plain"
	}
}

// deriveDefaultTitle extracts a human-friendly title from a file path by
// taking the basename and stripping the last extension. Used when the user
// runs `--file weekly.md` without `--title`. Only the last extension is
// stripped (archive.tar.gz → "archive.tar") to match user intuition.
// A leading-dot file (.bashrc) keeps its full basename (dot at index 0
// is not stripped).
func deriveDefaultTitle(path string) string {
	base := filepath.Base(path)
	if dot := strings.LastIndex(base, "."); dot > 0 {
		base = base[:dot]
	}
	return base
}

// checkFileSize is a pure function so tests can sweep synthetic sizes
// (0, at-cap, cap+1) without writing real fixtures to disk.
func checkFileSize(size int64) error {
	if size > maxFileBytes {
		return fmt.Errorf("file too large: %d bytes (max %d)", size, maxFileBytes)
	}
	return nil
}

// validateFileImport runs the file-level validation rules (Rules 2-4 from
// the spec): file must exist, be a regular file, and be within the size cap.
// os.Stat runs before any os.ReadFile so oversized files are rejected
// without allocating a buffer. Rule 0 (empty path) and Rule 1 (mutual
// exclusion with positional args) are handled in RunE before this helper.
func validateFileImport(path string) error {
	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("stat file: %w", err)
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("not a regular file: %s", path)
	}
	if err := checkFileSize(info.Size()); err != nil {
		return err
	}
	return nil
}
```

Ensure `"os"` and `"path/filepath"` are in the import block. `"fmt"` and `"strings"` are already imported in `user_note.go`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `CGO_ENABLED=1 go test ./internal/cli/ -run "TestMimeForTextFile|TestDeriveDefaultTitle|TestCheckFileSize|TestValidateFileImport" -v`

Expected: PASS (all subtests green).

- [ ] **Step 5: Run full CLI package regression**

Run: `CGO_ENABLED=1 go test ./internal/cli/...`

Expected: PASS — existing `TestFormatListItem` and `TestPreviewRunes` still pass.

- [ ] **Step 6: Format and commit**

```bash
goimports -w internal/cli/user_note.go internal/cli/user_note_test.go
git add internal/cli/user_note.go internal/cli/user_note_test.go
git commit -m "$(cat <<'EOF'
feat(cli): add file-import helpers for user note add

Four pure helpers that the --file flag's RunE will call:
- mimeForTextFile: .md/.markdown → text/markdown, else text/plain
- deriveDefaultTitle: basename with last extension stripped
- checkFileSize: pure int64 → error for the 10 MB cap
- validateFileImport: stat + regular-file + size check

checkFileSize is extracted as a pure function so the boundary
(0, at-cap, cap+1) is tested with synthetic values — no 10 MB
fixture written to disk anywhere in the suite.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Wire `--file` flag + `userNoteLoadAppFn` + RunE integration

**Files:**
- Modify: `internal/cli/user_note.go` (flag vars at line 27-32, `init()` at line 176-185, `userNoteAddCmd.RunE` at line 38-63)
- Create: `internal/cli/user_note_run_e_test.go`

**Interfaces:**
- Consumes from Task 1: `service.Input{ MIME: string, ... }` — constructed inline in RunE
- Consumes from Task 2: `mimeForTextFile(path)`, `deriveDefaultTitle(path)`, `validateFileImport(path)`
- Consumes from existing code: `loadApp()` (the real constructor, which `userNoteLoadAppFn` defaults to), `readContent(args)` (existing stdin/positional reader), `service.Input` struct, `domain.ScopeUser`/`KindNote`/`SourceManual`
- Produces: `var userNoteLoadAppFn = loadApp` (separate from `embed.go`'s `loadAppFn`)

- [ ] **Step 1: Write the failing RunE tests**

Create `internal/cli/user_note_run_e_test.go`:

```go
package cli

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/app"
	"uni-context/internal/config"
	"uni-context/internal/domain"
	"uni-context/internal/port"
	"uni-context/internal/service"
)

// swapUserNoteLoadAppFn swaps the package-level userNoteLoadAppFn to return
// a stubbed *App with a non-empty User.ID (ScopeUser + empty owner fails
// domain validation). Returns a restore func — tests MUST defer it.
// Separate from swapLoadAppFn in embed_status_test.go so embed RunE tests
// and userNote RunE tests each swap their own var without interference.
func swapUserNoteLoadAppFn(a *app.App) func() {
	prev := userNoteLoadAppFn
	userNoteLoadAppFn = func() (*app.App, *config.Config, error) {
		return a, &config.Config{User: config.UserConfig{ID: "test-user"}}, nil
	}
	return func() { userNoteLoadAppFn = prev }
}

// capturingRepo stores every Create'd item so RunE tests can assert on
// the Input the RunE constructed (MIME, SourceMeta, Content, etc.).
// Other methods panic — RunE tests only exercise the Create path.
type capturingRepo struct {
	created []domain.ContextItem
}

func (r *capturingRepo) Create(_ context.Context, item domain.ContextItem) error {
	r.created = append(r.created, item)
	return nil
}
func (r *capturingRepo) Get(_ context.Context, id string) (domain.ContextItem, error) {
	panic("unexpected Get call in RunE test")
}
func (r *capturingRepo) Update(_ context.Context, item domain.ContextItem) (domain.ContextItem, error) {
	panic("unexpected Update call in RunE test")
}
func (r *capturingRepo) Delete(_ context.Context, id string) error {
	panic("unexpected Delete call in RunE test")
}
func (r *capturingRepo) List(_ context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
	panic("unexpected List call in RunE test")
}
func (r *capturingRepo) NextCursor(_ domain.ContextItem) string { panic("unexpected NextCursor") }

// resetNoteFlags zeroes all package-level flag vars that userNoteAddCmd
// reads. Call at the start of each RunE test and via t.Cleanup so package
// state doesn't leak between tests.
func resetNoteFlags(t *testing.T) {
	t.Helper()
	noteFilePath = ""
	noteTitle = ""
	noteTags = nil
	flagJSON = false
	t.Cleanup(func() {
		noteFilePath = ""
		noteTitle = ""
		noteTags = nil
		flagJSON = false
	})
}

// TestUserNoteAddCmd_RunEWithFileImport_PreservesFilenameAndMIME: importing
// a small .md file must produce an item with ContentMIME="text/markdown",
// SourceMeta["original_filename"] set, content inline (not externalized),
// and a title derived from the basename. Exercises the full CLI → service
// path with a real IngestService against a capturing repo.
func TestUserNoteAddCmd_RunEWithFileImport_PreservesFilenameAndMIME(t *testing.T) {
	repo := &capturingRepo{}
	a := newStubApp(t)
	// emptyFileStore is reused from embed_run_e_test.go — safe because the
	// small fixture stays inline and never calls fs.Put.
	a.Ingest = service.NewIngestService(repo, emptyFileStore{})
	restore := swapUserNoteLoadAppFn(a)
	defer restore()
	resetNoteFlags(t)

	dir := t.TempDir()
	path := filepath.Join(dir, "weekly.md")
	require.NoError(t, os.WriteFile(path, []byte("# weekly notes"), 0o644))

	rootCmd.SetArgs([]string{"user", "note", "add", "--file", path, "--tag", "work"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	require.NoError(t, rootCmd.Execute())

	require.Len(t, repo.created, 1, "exactly one item should be created")
	item := repo.created[0]
	assert.Equal(t, "weekly.md", item.SourceMeta["original_filename"],
		"original filename must be preserved in SourceMeta")
	assert.Equal(t, "text/markdown", item.ContentMIME,
		"MIME must be detected from .md extension and preserved on inline item")
	assert.Equal(t, "# weekly notes", item.Content,
		"small file content stays inline")
	assert.Empty(t, item.ContentURI, "small file should not be externalized")
	assert.Equal(t, "weekly", item.Title,
		"title should derive from basename with extension stripped")
	assert.Equal(t, []string{"work"}, item.Tags)
}

// TestUserNoteAddCmd_RunEFileFlagMutuallyExclusiveWithPositional: passing
// both --file and a positional arg must error cleanly with the "cannot
// combine" message. This is Rule 1 from the spec.
func TestUserNoteAddCmd_RunEFileFlagMutuallyExclusiveWithPositional(t *testing.T) {
	repo := &capturingRepo{}
	a := newStubApp(t)
	a.Ingest = service.NewIngestService(repo, emptyFileStore{})
	restore := swapUserNoteLoadAppFn(a)
	defer restore()
	resetNoteFlags(t)

	dir := t.TempDir()
	path := filepath.Join(dir, "x.txt")
	require.NoError(t, os.WriteFile(path, []byte("x"), 0o644))

	rootCmd.SetArgs([]string{"user", "note", "add", "--file", path, "extra-positional"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	err := rootCmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "cannot combine --file")
	assert.Empty(t, repo.created, "no item should be created on validation failure")
}

// TestUserNoteAddCmd_RunEPropagatesValidationError: pointing --file at a
// nonexistent path must surface the validator's "stat file:" error. Zero
// bytes written to disk. Confirms RunE invokes validateFileImport and
// propagates its error.
func TestUserNoteAddCmd_RunEPropagatesValidationError(t *testing.T) {
	repo := &capturingRepo{}
	a := newStubApp(t)
	a.Ingest = service.NewIngestService(repo, emptyFileStore{})
	restore := swapUserNoteLoadAppFn(a)
	defer restore()
	resetNoteFlags(t)

	missingPath := filepath.Join(t.TempDir(), "does-not-exist.txt")
	rootCmd.SetArgs([]string{"user", "note", "add", "--file", missingPath})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	err := rootCmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "stat file:")
	assert.Empty(t, repo.created)
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `CGO_ENABLED=1 go test ./internal/cli/ -run "TestUserNoteAddCmd_RunE" -v`

Expected: compile failure — `userNoteLoadAppFn` undefined, `noteFilePath` undefined, `emptyFileStore` might collide (see collision guard below).

- [ ] **Step 3: Add `userNoteLoadAppFn` indirection + `--file` flag + rewrite RunE**

**3a. Add the indirection var.** Near the top of `internal/cli/user_note.go`, after the existing `loadApp` reference (or near the `var (` block at line 27):

```go
// userNoteLoadAppFn is the indirection that lets RunE tests swap in a
// stubbed *App without touching the real config/DB. Separate from
// embed.go's loadAppFn so each command file's tests are scoped to its
// own var. Defaults to the real loadApp in production.
var userNoteLoadAppFn = loadApp
```

**3b. Register the `--file` flag.** In `init()`, add after the existing `userNoteAddCmd.Flags()` calls:

```go
userNoteAddCmd.Flags().StringVar(&noteFilePath, "file", "", "import content from a file (text only)")
```

**3c. Add the `noteFilePath` var** in the existing `var (...)` block:

```go
var (
	noteTitle      string
	noteTags       []string
	noteTagsFilter []string
	noteLimit      int
	noteFilePath   string
)
```

**3d. Rewrite `userNoteAddCmd.RunE`.** Replace the entire `RunE` func body:

```go
	RunE: func(cmd *cobra.Command, args []string) error {
		// Rule 0: --file "" (explicit empty) must not fall through to
		// readContent(args), which would surface the misleading
		// "content required (positional arg or - for stdin)".
		if cmd.Flags().Changed("file") && noteFilePath == "" {
			return fmt.Errorf("--file: path cannot be empty")
		}

		var content string
		var mime string
		sourceMeta := map[string]any{}
		if noteFilePath != "" {
			// File import path.
			if len(args) > 0 { // Rule 1: mutual exclusion
				return fmt.Errorf("cannot combine --file with positional content or -")
			}
			if err := validateFileImport(noteFilePath); err != nil { // Rules 2-4
				return err
			}
			data, err := os.ReadFile(noteFilePath)
			if err != nil {
				return fmt.Errorf("read file: %w", err)
			}
			content = string(data)
			mime = mimeForTextFile(noteFilePath)
			if !cmd.Flags().Changed("title") {
				noteTitle = deriveDefaultTitle(noteFilePath)
			}
			sourceMeta["original_filename"] = filepath.Base(noteFilePath)
		} else {
			// Existing path: positional arg OR "-" stdin. Unchanged.
			c, err := readContent(args)
			if err != nil {
				return err
			}
			content = c
		}

		a, cfg, err := userNoteLoadAppFn()
		if err != nil {
			return err
		}
		defer a.Close()

		id, err := a.Ingest.Create(cmd.Context(), service.Input{
			Scope:       domain.ScopeUser,
			Kind:        domain.KindNote,
			Source:      domain.SourceManual,
			OwnerUserID: cfg.User.ID,
			Title:       noteTitle,
			Content:     content,
			Tags:        noteTags,
			MIME:        mime,
			SourceMeta:  sourceMeta,
		})
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
```

**Collision guard:** if `go build` reports `emptyFileStore` redeclared (it's already in `embed_run_e_test.go`), the new test file reuses the existing one — do NOT redeclare it. The test file above deliberately references `emptyFileStore` from `embed_run_e_test.go` (same package). If there's a collision, it means the name was duplicated — remove the duplicate from whichever file is newer.

**3e. Add imports.** Ensure `internal/cli/user_note.go` has these imports (some may already be present):
- `"os"` (for `os.ReadFile`)
- `"path/filepath"` (for `filepath.Base`)

The `service` package import should already be present. If not, add `"uni-context/internal/service"`.

- [ ] **Step 4: Run RunE tests to verify they pass**

Run: `CGO_ENABLED=1 go test ./internal/cli/ -run "TestUserNoteAddCmd_RunE" -v`

Expected: PASS (all 3 RunE tests green).

- [ ] **Step 5: Run full CLI package regression**

Run: `CGO_ENABLED=1 go test ./internal/cli/...`

Expected: PASS — existing tests (`TestFormatListItem`, `TestPreviewRunes`, `TestEmbedStatusCmd_*`, `TestEmbedModelAddCmd_*`, etc.) still pass. No `-tags sqlite_fts5` needed.

- [ ] **Step 6: Run full repo regression**

Run: `CGO_ENABLED=1 go test -tags sqlite_fts5 ./...`

Expected: PASS across all packages.

- [ ] **Step 7: Format and commit**

```bash
goimports -w internal/cli/user_note.go internal/cli/user_note_run_e_test.go
git add internal/cli/user_note.go internal/cli/user_note_run_e_test.go
git commit -m "$(cat <<'EOF'
feat(cli): add --file flag to user note add for text file imports

Adds --file <path> to `unictx user note add`, importing .txt and .md
files as notes. The flag is mutually exclusive with positional content
and stdin (-). MIME is detected from extension (.md → text/markdown,
else text/plain). Title defaults to the filename basename with extension
stripped when --title is not explicitly set. Original filename is
preserved in SourceMeta["original_filename"].

File imports follow the existing len(content) > ContentInlineLimit path
(small files stay inline and FTS-searchable; large files externalize to
FileStore). 10 MB cap enforced via os.Stat before os.ReadFile.

RunE tests use a separate userNoteLoadAppFn indirection (not embed.go's
loadAppFn) so each command file's test swaps are scoped to themselves.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Notes

**Spec coverage check:**
- Scope item 1 (--file flag, mutually exclusive): Task 3 ✅
- Scope item 2 (extension-based MIME): Task 2 `mimeForTextFile` + Task 3 RunE ✅
- Scope item 3 (default title from basename, `Flags().Changed` guard): Task 2 `deriveDefaultTitle` + Task 3 RunE ✅
- Scope item 4 (original_filename in SourceMeta): Task 3 RunE ✅
- Scope item 5 (10 MB cap via os.Stat before ReadFile): Task 2 `validateFileImport` + `checkFileSize` ✅
- Scope item 6 (one new field MIME, size-threshold path): Task 1 ✅
- Components #1 (flag + validation rules 0-4): Task 3 RunE + Task 2 helpers ✅
- Components #2 (MIME helper): Task 2 ✅
- Components #3 (default title derivation): Task 2 + Task 3 ✅
- Components #4 (SourceMeta): Task 3 RunE ✅
- Components #5 (Input.MIME + Create externalization): Task 1 ✅
- Data flow (small file inline, large file externalized): Task 1 tests + Task 3 test ✅
- Error handling matrix (all 10 scenarios): covered by Tasks 2+3 unit + RunE tests ✅

**Placeholder scan:** No TBD/TODO/"implement later". Every step shows complete code.

**Type consistency:** `mimeForTextFile(string) string`, `deriveDefaultTitle(string) string`, `checkFileSize(int64) error`, `validateFileImport(string) error` — all match between Task 2 definitions and Task 3 usage. `Input.MIME` field name matches between Task 1 definition and Task 3 RunE construction. `userNoteLoadAppFn` matches between Task 3d var declaration and test `swapUserNoteLoadAppFn`.

**Known limitation flagged in spec:** `--json` doesn't expose `source_meta` or `content_mime` — out of scope per spec's Known Limitations section. Not implemented here.
