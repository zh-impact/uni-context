# User Note File Import — Design

**Date:** 2026-06-23
**Scope:** Add `--file <path>` flag to `unictx user note add` to import text files as notes. Text-only for now; binary (image/PDF) imports are explicitly deferred.

## Motivation

Today's `user note add [content|-]` only accepts inline text or stdin. Users who keep notes in files (`.txt`, `.md`) have to either cat-into-stdin or paste the body on the command line. For notes that already exist as files, neither path is ergonomic; both lose the original filename and the file-type information (`.md` vs `.txt`) that downstream renderers care about.

The minimum useful improvement: let the user point at a file and have the existing note pipeline (IngestService → FileStore → FTS index → embeddings) handle it. Scope intentionally limited to text so MIME detection, validation, and UX stay simple; binary support can layer on later without redesign.

## Scope

**In scope:**
1. `--file <path>` flag on `userNoteAddCmd`. Mutually exclusive with positional content + `-` stdin.
2. Extension-based MIME detection for known text types: `.md`/`.markdown` → `text/markdown`, everything else → `text/plain`.
3. Default title derivation: when `--title` empty, use file basename with extension stripped (e.g. `weekly.md` → `"weekly"`).
4. Original filename preserved in `item.SourceMeta["original_filename"]`.
5. File size cap: 10 MB. Enforced via `os.Stat` before `os.ReadFile` so rejected files never allocate a buffer.
6. Force externalization to FileStore regardless of size — file imports are durable as files, not inline strings.
7. Two new fields on `service.Input`: `MIME string`, `ForceExternalize bool`.

**Out of scope:**
- Binary file imports (images, PDFs, anything where text/plain or text/markdown is wrong).
- File upload via stdin with `--file -` (stdin already works without MIME detection).
- File-content validation (is this actually valid UTF-8?). The FileStore stores bytes; consumers that need valid UTF-8 already cast and check.
- Async file processing, batch imports, watch-directory mode — Plan 2d+ territory.
- Subprocess e2e tests for the new flag (existing patterns cover this; covered by RunE-level tests instead).

## Architecture

The change is small and localized. No domain changes, no new packages, no new dependencies. Two layers touch:

- **CLI (`internal/cli/user_note.go`)** — adds the `--file` flag, file-I/O + validation, MIME detection, default-title derivation, and SourceMeta population. Bytes are read at the CLI layer (consistent with existing `readContent` reading stdin there).
- **Service (`internal/service/ingest.go`)** — `Input` struct gains `MIME string` and `ForceExternalize bool`. `Create` uses them when externalizing.

`KindNote` stays the same (file imports are notes; a note is a note regardless of how content arrived). `SourceManual` stays the same (user-initiated). `SourceMeta` already exists as `map[string]any` and is the natural home for `original_filename`.

## Components

### 1. CLI flag + validation (`internal/cli/user_note.go`)

Add a new package-level flag var and register it on `userNoteAddCmd`:

```go
var noteFilePath string

// In init():
userNoteAddCmd.Flags().StringVar(&noteFilePath, "file", "", "import content from a file (text only)")
```

The flag is reset between test runs via the `t.Cleanup` pattern established in `embed_status_test.go` (e.g. `t.Cleanup(func() { noteFilePath = "" })`). RunE-level tests that touch `noteTitle`/`noteTags` reset those too.

`userNoteAddCmd.RunE` reshapes to handle three input modes: positional text, `-` stdin, `--file <path>`. The validation rules below enforce mutual exclusion.

**Validation rules (in order):**

| # | Condition | Error |
|---|---|---|
| 1 | `--file` set AND (`len(args) > 0` OR `args[0] == "-"`) | `"cannot combine --file with positional content or -"` |
| 2 | File does not exist | wrapped `os.Stat` error: `"stat file: <err>"` |
| 3 | File is not a regular file (directory, socket, device) | `"not a regular file: <path>"` |
| 4 | File size > 10 MB | `"file too large: <N> bytes (max 10485760)"` |

`os.Stat` runs BEFORE `os.ReadFile` so a 12 MB file is rejected without ever allocating a 12 MB buffer.

Empty files are allowed; the result is a note with empty content, externalized to FileStore (size 0, refcount 1). This matches the existing behavior for an empty positional string.

### 2. MIME detection helper

Unexported function next to `readContent` in `internal/cli/user_note.go`:

```go
// mimeForTextFile maps a small set of text file extensions to MIME types.
// Unknown extensions default to text/plain — binary support is out of scope.
// Adding new text types later (org, rst) is a one-liner here.
func mimeForTextFile(path string) string {
    switch strings.ToLower(filepath.Ext(path)) {
    case ".md", ".markdown":
        return "text/markdown"
    default:
        return "text/plain"
    }
}
```

`.txt` and unknown extensions both fall through to `text/plain` — the safe default. Case-insensitive via `strings.ToLower` so `weekly.MD` is still recognized as Markdown.

### 3. Default title derivation

When `--title` is empty AND `--file` is set, derive the title from the file's basename with the extension stripped:

```go
if noteTitle == "" && noteFilePath != "" {
    base := filepath.Base(noteFilePath)
    if dot := strings.LastIndex(base, "."); dot > 0 {
        base = base[:dot]
    }
    noteTitle = base
}
```

- `weekly.md` → `"weekly"`
- `notes.txt` → `"notes"`
- `noext` → `"noext"` (no dot found, basename unchanged)
- `.bashrc` → `".bashrc"` (dot is at index 0; `dot > 0` guards against stripping the leading dot)
- `archive.tar.gz` → `"archive.tar"` (only the last extension stripped — matches the "what the user typed" intuition)

### 4. SourceMeta: original filename

CLI layer populates `SourceMeta["original_filename"]` with the basename. This is the only SourceMeta key file imports write; future extensions (e.g. `imported_at`, `source_app`) can add their own keys.

```go
sourceMeta := map[string]any{}
if noteFilePath != "" {
    sourceMeta["original_filename"] = filepath.Base(noteFilePath)
}
```

`inputFromFlags` (existing helper) is **not extended**. Today it has a single caller — `userNoteAddCmd.RunE` — and growing its parameter list to carry SourceMeta, MIME, and ForceExternalize would make the helper unwieldy. Instead, `userNoteAddCmd.RunE` constructs `service.Input` directly with all fields. `inputFromFlags` is left untouched for any future caller that doesn't need the new fields.

### 5. IngestService.Input additions (`internal/service/ingest.go`)

Two new fields:

```go
type Input struct {
    // ... existing fields unchanged ...

    Content string
    Tags    []string

    // MIME of the content. Empty means "text/plain" (preserves existing
    // behavior for inline/stdin notes — no caller update required).
    // Set by the CLI when importing a .md file so FileStore and the
    // ContextItem both carry the right MIME.
    MIME string

    // ForceExternalize bypasses the inline-content path. Used for file
    // imports: keeps file-origin notes durable in FileStore regardless
    // of size, so "did this come from a file?" is answerable via
    // item.ContentURI != "" without consulting SourceMeta.
    ForceExternalize bool
}
```

The `Create` method's externalization block becomes:

```go
mime := in.MIME
if mime == "" {
    mime = "text/plain"
}
if in.ForceExternalize || len(in.Content) > domain.ContentInlineLimit {
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
    // item.ContentMIME stays empty for inline — matches Plan 1 behavior.
    // Existing `user note get` returns content as a string regardless.
}
```

Behavior preserved for all existing callers (inline text + stdin): `MIME == ""` and `ForceExternalize == false`, so the inline-vs-externalize decision is unchanged.

## Data flow

### Happy path: `unictx user note add --file weekly.md --tag work`

```
CLI parses --file=weekly.md
→ validate: regular file, size ≤ 10 MB
→ os.ReadFile(weekly.md) → []byte
→ mimeForTextFile → "text/markdown"
→ noteTitle empty → derive from basename → "weekly"
→ SourceMeta["original_filename"] = "weekly.md"
→ IngestService.Create(Input{
      Scope: user, Kind: note, Source: manual,
      Content: string(bytes), MIME: "text/markdown",
      ForceExternalize: true, Tags: ["work"],
      SourceMeta: {"original_filename": "weekly.md"},
  })
→ Create: MIME="text/markdown", ForceExternalize=true → skip inline path
→ fs.Put(bytes, "text/markdown") → FileStore sha256-addresses bytes
→ writes meta{refcount:1, mime:"text/markdown", size:N}
→ item.ContentURI = "file://abc123...", item.ContentMIME = "text/markdown",
   item.Content = ""
→ repo.Create(item)
→ embed service runs (existing Plan 2a path) — hydrates via ContentURI
→ returns item.ID
→ CLI prints "added: <id>"
```

### `user note get <id>` on a file-imported note

Existing flow already handles this at `internal/cli/user_note.go:125-132`: `item.Content == ""` triggers `a.FS.Get(item.ContentURI)` and casts to string. No change needed. The original filename is recoverable from `SourceMeta["original_filename"]` if a future `--show-meta` flag is added (out of scope here).

## Error handling matrix

| Scenario | Behavior |
|---|---|
| `--file foo.txt` + positional content or `-` | Exit 1: `"cannot combine --file with positional content or -"` |
| `--file /nonexistent/path` | Exit 1: `"stat file: stat /nonexistent/path: no such file or directory"` |
| `--file /some/directory` | Exit 1: `"not a regular file: /some/directory"` |
| `--file huge.log` (12 MB) | Exit 1: `"file too large: 12582912 bytes (max 10485760)"` |
| `--file notes.md` (empty file) | Allowed — creates a note with empty content, externalized |
| Unknown extension `.org` | MIME defaults to `text/plain`; note succeeds |
| `--file notes.md` with no `--title` | Title defaults to `"notes"` |
| `--file .bashrc` (hidden, leading-dot) | Title defaults to `".bashrc"` (full basename) |
| `--file archive.tar.gz` | Title defaults to `"archive.tar"` (only last extension stripped) |
| File read succeeds but `repo.Create` fails | Existing rollback path: `fs.Delete(ContentURI)` decrements refcount (unchanged) |
| `--file weekly.MD` (uppercase ext) | MIME = `"text/markdown"` (case-insensitive via `strings.ToLower`) |

## Testing plan

### Unit tests (`internal/cli/user_note_test.go` — append to existing file)

Table-driven tests for the two pure helpers:

```go
func TestMimeForTextFile(t *testing.T) {
    cases := []struct{ path, want string }{
        {"notes.txt", "text/plain"},
        {"weekly.md", "text/markdown"},
        {"weekly.markdown", "text/markdown"},
        {"weekly.MD", "text/markdown"},       // case-insensitive
        {"notes.org", "text/plain"},           // unknown → default
        {"noext", "text/plain"},               // no extension
        {".bashrc", "text/plain"},             // leading-dot, no real ext
    }
    for _, c := range cases {
        assert.Equal(t, c.want, mimeForTextFile(c.path))
    }
}

func TestDeriveDefaultTitle(t *testing.T) {
    cases := []struct{ path, want string }{
        {"weekly.md", "weekly"},
        {"notes.txt", "notes"},
        {"noext", "noext"},
        {".bashrc", ".bashrc"},
        {"archive.tar.gz", "archive.tar"},     // only last ext stripped
        {"/abs/path/notes.md", "notes"},        // basename only
    }
    for _, c := range cases {
        assert.Equal(t, c.want, deriveDefaultTitle(c.path))
    }
}
```

The helpers are extracted as named functions (`mimeForTextFile`, `deriveDefaultTitle`) rather than inlined in RunE so they're independently testable.

### Validation unit tests (same file)

- `TestValidateFilePath_NotExisting` — `--file /no/such` → error contains `"stat file:"`.
- `TestValidateFilePath_Directory` — `--file /tmp` (or `t.TempDir()`) → `"not a regular file"`.
- `TestValidateFileSize_TooLarge` — fixture file > cap (use `t.TempDir` + write N+1 bytes) → `"file too large"`.

These wrap the validation rules in a small unexported `validateFileImport(path string) (size int64, err error)` helper, again for direct testability.

### Service tests (`internal/service/ingest_test.go` — append)

- `TestIngestService_Create_ForceExternalizeRoutesToFS` — `ForceExternalize=true` with 100-byte content; assert `item.ContentURI != ""`, `item.Content == ""`, `item.ContentMIME == "text/markdown"` (when `MIME` set).
- `TestIngestService_Create_DefaultMIMEIsTextPlain` — `MIME` empty + `ForceExternalize=true`; assert FileStore `.meta` records `"text/plain"`.
- `TestIngestService_Create_InlineUnchangedWhenForceFalse` — regression guard: small content + `ForceExternalize=false` stays inline (ContentURI empty, Content populated).

The existing `fakeEmbedRepo`/`noopVectorStore` test helpers in `internal/service/reembed_test.go` are reused (they're package-private but accessible from `ingest_test.go` in the same package).

### CLI RunE-level integration

Two paths considered:

(a) Add `loadAppFn` indirection to `user_note.go` (mirroring `embed.go`) and write RunE tests with a stubbed `*App`. Cleanest, but introduces an indirection used only by tests.

(b) Subprocess e2e via the existing `internal/cli/e2e_test.go` pattern: write a fixture file under `t.TempDir()`, invoke `unictx user note add --file <fixture> --json`, parse JSON for the ID, then invoke `unictx user note get <id> --json` and assert content + original_filename.

**Decision: (a).** The `loadAppFn` pattern is already established in `embed.go` from Plan 2c follow-up Task 5; applying it to `user_note.go` is one extra line and unlocks focused RunE tests without subprocess overhead. Subprocess e2e can be added later for golden-path coverage if needed.

Tests added to a new `internal/cli/user_note_run_e_test.go` (or appended to `user_note_test.go`):

- `TestUserNoteAddCmd_RunEWithFileImport_ExternalizesAndPreservesFilename` — fixture `.md` file, stub App wires a real `IngestService` against an in-memory repo + a temp-dir FileStore. Assert item has ContentURI set, ContentMIME = `"text/markdown"`, SourceMeta has `original_filename`.
- `TestUserNoteAddCmd_RunEFileFlagMutuallyExclusiveWithPositional` — `--file x.txt abc` → error contains `"cannot combine --file"`.
- `TestUserNoteAddCmd_RunEFileFlagRejectsLargeFile` — fixture > 10 MB → `"file too large"`.

### Out of scope

- Subprocess e2e for the new flag.
- Tests for binary file rejection (no binary support yet).
- Load-testing the 10 MB cap (single-threaded unit test is sufficient).

## Risks

1. **`ForceExternalize` semantics drift.** The flag's intent is "this content came from a file". A future caller that sets `ForceExternalize=true` for non-file content would break the `item.ContentURI != ""` ⟹ `"file origin"` invariant. Mitigation: the field's godoc is explicit about the contract, and the only caller is `userNoteAddCmd.RunE`.

2. **MIME allow-list vs. extension deny-list.** The chosen design maps known text extensions to MIMEs and defaults everything else to `text/plain`. A user importing a `.html` file gets `text/plain` — the file imports successfully but downstream renderers won't know it's HTML. Trade-off: simpler than a deny-list (which would have to enumerate every binary type), and HTML support can be added later by extending the switch.

3. **Filename sanitization.** `filepath.Base(noteFilePath)` returns the basename as-is. On Unix this is safe; on Windows a path with `\` would yield an unexpected basename. The project targets darwin/Linux (no Windows in CI), so this is documented as a known limitation rather than fixed.

4. **Race with concurrent file modification.** Between `os.Stat` (size check) and `os.ReadFile`, the file could grow. Result: a >10 MB file might slip through if it was truncated-then-grown between the two calls. Mitigation: acceptable — the cap is a guardrail against accidentally loading huge files, not a security boundary.

5. **`loadAppFn` test pattern coupling.** Adding the indirection to `user_note.go` widens the pattern beyond `embed.go`. This is consistent with the established convention but if the codebase later moves to dependency injection for the whole CLI, the indirection becomes redundant. Low risk; easy to remove.

## Out of scope (forward-compat)

- Binary file imports (images, PDFs, audio) — requires MIME sniffing, binary-safe FileStore validation, and per-kind render strategies.
- Streamed stdin upload with `--file -` (stdin already works without MIME detection via the existing `-` positional).
- File-content type sniffing (e.g. `http.DetectContentType` on the first 512 bytes) — overkill for text-only scope.
- Async file processing / batch import — Plan 2d+ scope.
- Filename-based Kind inference (e.g. `.pdf` → `KindDoc`).
- Watch-directory mode (auto-import on file creation).
