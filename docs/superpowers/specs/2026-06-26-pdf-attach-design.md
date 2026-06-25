# PDF Attach for `user note add` — Design

**Date:** 2026-06-26
**Status:** Approved (brainstorming complete), pending implementation plan
**Owner:** zead

## Context & Goal

`user note add` currently accepts text imports via `--file` (`.md`, `.txt`).
PDFs — the dominant format for academic papers, manuals, and archived
documents — are rejected (the binary MIME falls into the "out of scope"
branch of `mimeForTextFile`).

This feature adds first-class PDF support: pass `--file paper.pdf`, get
a context item whose body is the extracted text and whose original PDF
bytes are retained in the FileStore for later retrieval. Extraction is
pluggable so users can choose between a pure-Go default (`gxpdf`), an
external shell tool (`pdftotext`), or an HTTP service.

**Goal:** `unictx user note add --file paper.pdf` produces a searchable,
embeddable context item backed by both the extracted text and the
original PDF blob.

## Non-goals (out of scope)

- Encrypted PDF support (`--password` flag). The gxpdf adapter detects
  encryption and returns a clear error; a follow-up can wire
  `gxpdf.OpenWithPassword` through a `--password` flag.
- Page-range selection (`--pages 1-10`). gxpdf extracts all pages.
- Other binary formats (docx, html, images with OCR). The port is
  named `PDFExtractor` precisely to keep scope narrow; a future
  `DocumentExtractor` could generalize.
- `--no-size-limit` escape hatch for files > 50MB.
- Per-page metadata (`original_pages`) and content-hash metadata
  (`original_hash` in SourceMeta). Both are recoverable later if a use
  case emerges.

## Architecture

```
┌─────────────────────┐
│  CLI                │  user_note.go
│  --file paper.pdf   │  --engine shell (optional override)
└──────────┬──────────┘
           │  service.WithExtractor(ext)  (CreateOption)
           ▼
┌─────────────────────────────────────────────────────────────┐
│  IngestService.Create(ctx, input, opts...) (string, error)  │
│  ───────────────────────────────────────                    │
│  if Input.MIME == "application/pdf":                       │
│    if extractor == nil → error "pdf extraction not          │
│      configured: set pdf.engine in config or pass --engine" │
│    1. text, err := extractor.Extract(ctx, []byte(in.Content)) │
│    2. pdfURI, _ := fs.Put([]byte(in.Content),               │
│                          "application/pdf")                 │
│    3. in.SourceMeta["original_uri"]   = pdfURI              │
│       in.SourceMeta["original_mime"]  = "application/pdf"   │
│       in.Content = text   // "" on image-only               │
│       in.MIME    = "text/plain"                             │
│  ───────────────────────────────────────                    │
│  (existing flow: externalize > 4KB → repo → FTS → embed)    │
│  (rollback extended: on repo.Create failure, also fs.Delete │
│   pdfURI from SourceMeta["original_uri"])                   │
└─────────────────────────────────────────────────────────────┘
```

The CLI is the single entry point today; engine selection is driven by
CLI flag (`A1` — per-call extractor override). The CLI builds the
extractor instance from config + `--engine`, passes it via
`service.WithExtractor(ext)` to a single `Create` call. The service
itself has no engine-name knowledge.

**Return type note:** `Create` returns `(string, error)` — only the
item ID, not the full `domain.ContextItem`. This matches the existing
signature (ingest.go:63). PDF branch must `return "", err` on failure.

**Rollback extension (load-bearing):** the existing rollback at
ingest.go:111-113 only deletes `item.ContentURI` (the externalized
*text* blob). The PDF branch adds a *second* blob (`pdfURI` in
SourceMeta["original_uri"]) that must also be cleaned up if
`repo.Create` fails. Implementation must capture `pdfURI` in a
variable visible to the rollback block. Test
`Create_PDF_RollsBackBothBlobsOnRepoFailure` pins this.

## Components

| Layer | File | Responsibility |
|---|---|---|
| `port` | `internal/port/pdf.go` (new) | `PDFExtractor` interface + error semantics |
| `adapter` | `internal/adapter/pdf/gxpdf.go` (new) | Pure-Go default engine |
| `adapter` | `internal/adapter/pdf/shell.go` (new) | `exec.Command` wrapper |
| `adapter` | `internal/adapter/pdf/http.go` (new) | HTTP POST binary extractor |
| `app` | `internal/app/pdf.go` (new) | `BuildPDFExtractor` / `BuildExtractorForEngine` factories used by Wire |
| `service` | `internal/service/ingest.go` (modify) | PDF branch + `WithExtractor` `CreateOption` (per-call) + `WithPDFExtractor` `IngestOption` (constructor) |
| `cli` | `internal/cli/user_note.go` (modify) | Accept `.pdf`, parse `--engine`, route to service |
| `config` | `internal/config/config.go` (modify) | `PDFConfig` block — added to existing `Config` struct alongside `EmbedderConfig` |

## Domain model

**No changes.** `ContextItem` already has `Content`, `ContentURI`,
`ContentMIME`, and `SourceMeta`. The PDF branch reuses those fields:

- `Content` — extracted text (empty when extraction yielded nothing)
- `ContentURI` — extracted-text blob URI when externalized (> 4 KB),
  or empty when text fits inline or extraction was empty
- `SourceMeta["original_uri"]` — original PDF blob URI (always set on
  PDF items, even when extraction succeeded inline)
- `SourceMeta["original_mime"]` — `"application/pdf"`

The asymmetry between `ContentURI` (extracted text) and
`SourceMeta["original_uri"]` (PDF blob) is intentional: existing
semantics for `ContentURI` are preserved, and the PDF blob address is
captured in metadata so any future "download original" feature can find
it without a schema migration.

## Port: `PDFExtractor`

```go
// internal/port/pdf.go
package port

import "context"

// PDFExtractor extracts plain text from a PDF document.
//
// Empty extraction (image-only/scanned PDF, no text layer) returns
// ("", nil) — NOT an error. Callers decide how to handle empty text
// per their UX; the user-note-add flow stores the PDF blob with empty
// Content in this case.
//
// Actual failures (malformed PDF, encrypted, IO error, downstream
// HTTP 5xx) return ("", err). Callers SHOULD surface these to the user.
type PDFExtractor interface {
    Extract(ctx context.Context, content []byte) (text string, err error)
}
```

The empty-vs-error contract is load-bearing: it lets `IngestService`
treat "image-only PDF" as a successful flow with degraded UX rather
than a special-case failure.

## Adapters

### `GxpdfExtractor` (`internal/adapter/pdf/gxpdf.go`)

Wraps `github.com/coregx/gxpdf`. Uses `gxpdf.NewReader` on a
`bytes.Reader` over the input. Iterates `doc.Pages()`, accumulating
`page.ExtractText()`. Per-page errors are logged via the injected
`io.Writer` and skipped; successful pages are still returned (partial
extraction is more useful than total failure).

```go
func NewGxpdfExtractor(log io.Writer) *GxpdfExtractor
```

- Encrypted PDF (no password provided) → returns error containing
  `"encrypted"` so callers can detect and message clearly.
- **Dependency verified 2026-06-26:**
  [github.com/coregx/gxpdf](https://github.com/coregx/gxpdf) —
  pure Go, MIT, requires Go 1.25+. API surface:
  `gxpdf.NewReader(r io.ReaderAt, size int64) (*Document, error)`,
  `doc.Pages() []Page`, `page.ExtractText() (string, error)`,
  plus `OpenWithPassword(path, password string)` for encrypted PDFs
  (deferred — out of scope per Non-goals). Project `go.mod` already
  runs Go 1.25, so the version constraint is satisfied.
  - *Risk note:* not as widely adopted as `pdfcpu` or
    `ledongthuc/pdf`, but the API is clean, MIT-licensed, and the
    shell/http engines provide a fallback if extraction quality
    proves inadequate.

### `ShellExtractor` (`internal/adapter/pdf/shell.go`)

Spawns a subprocess via `exec.CommandContext`. Writes PDF bytes to
stdin, reads extracted text from stdout. Shell-style command is split
via `strings.Fields` (no `sh -c` interpretation — keeps quoting
simple). The expected pattern is `pdftotext - -` (stdin → stdout).

```go
func NewShellExtractor(command string, timeout time.Duration) *ShellExtractor
```

- Config: `command` (string), `timeout` (default 30s if zero).
- Exit code != 0 → error includes exit code + trimmed stderr.
- Timeout → kills process, wraps `context.DeadlineExceeded`.
- Binary not in PATH → wraps the `exec.LookPath` error.
- Limitation: no shell features (pipes, redirects, globs). Users who
  need them wrap their pipeline in a script and point `command` at
  the script path.

### `HttpExtractor` (`internal/adapter/pdf/http.go`)

POSTs the PDF bytes with `Content-Type: application/pdf` to the
configured URL. Expects response `Content-Type: text/plain` and reads
the body as UTF-8 text.

```go
func NewHttpExtractor(url string, timeout time.Duration, authToken string) *HttpExtractor
```

- Config: `url`, `timeout` (default 30s if zero), `auth_token`
  (optional, sent as `Authorization: Bearer <token>`).
- Non-2xx → error with status + first 256 bytes of body.
- Timeout → wraps `context.DeadlineExceeded`.
- Wrong response MIME → error naming expected vs actual.

## Service changes

### Constructor (hybrid: existing pairs + variadic)

The existing `NewIngestService` + `NewIngestServiceWithEmbedder` pair
gains a trailing variadic `opts ...IngestOption` so PDF support is
opt-in and existing call sites / tests don't break:

```go
type IngestOption func(*IngestService)

// WithPDFExtractor configures PDF → text extraction. Without it,
// passing Input with MIME "application/pdf" returns an error.
func WithPDFExtractor(ext port.PDFExtractor) IngestOption {
    return func(s *IngestService) { s.pdfExtractor = ext }
}

func NewIngestService(repo port.ContextRepo, fs port.FileStore, log io.Writer, opts ...IngestOption) *IngestService
func NewIngestServiceWithEmbedder(repo port.ContextRepo, fs port.FileStore, embed *EmbedService, log io.Writer, opts ...IngestOption) *IngestService
```

### Per-call override (A1) — separate option type

```go
// CreateOption configures one Create call. Overrides constructor default.
type CreateOption func(*createConfig)

// WithExtractor overrides the service's constructor-configured
// extractor for this single call. Used when the CLI passes --engine.
//
// NOTE on the name divergence from WithPDFExtractor (the IngestOption
// variant): Go does not support function overloading — two top-level
// functions in the same package cannot share a name, even with
// different signatures. "Per-type scoping" only applies to methods on
// different receiver types, not package-level functions. The two
// options intentionally have different names: WithPDFExtractor for
// the constructor, WithExtractor for the per-call override.
func WithExtractor(ext port.PDFExtractor) CreateOption {
    return func(c *createConfig) { c.extractor = ext }
}

func (s *IngestService) Create(ctx context.Context, in Input, opts ...CreateOption) (string, error)
```

Two distinct option types because they have different lifetimes:
`IngestOption` is service-wide; `CreateOption` is per-call. The
naming asymmetry is a deliberate workaround for Go's lack of
overloading, not an inconsistency to fix.

### Branching logic

```go
func (s *IngestService) Create(ctx context.Context, in Input, opts ...CreateOption) (string, error) {
    cfg := createConfig{extractor: s.pdfExtractor}
    for _, opt := range opts { opt(&cfg) }

    // pdfURI is captured here so the repo.Create rollback below can
    // clean up the PDF blob too. Empty when no PDF branch ran.
    var pdfURI string

    if in.MIME == "application/pdf" {
        if cfg.extractor == nil {
            return "", fmt.Errorf(
                "pdf extraction not configured: set pdf.engine in config or pass --engine")
        }
        // SourceMeta nil-guard: the PDF branch writes to in.SourceMeta
        // BEFORE the existing nil-check at ingest.go:79-81 runs (that
        // check protects item.SourceMeta, built later). The CLI always
        // initializes the map, but a future API caller passing nil
        // would panic on in.SourceMeta["original_uri"] = pdfURI.
        // Defensive: ensure the map exists.
        if in.SourceMeta == nil {
            in.SourceMeta = map[string]any{}
        }
        // PDF bytes arrive as string via Input.Content (CLI reads file as
        // []byte then casts to string). Cast back for the extractor and
        // FileStore — both take []byte. Pattern matches existing
        // externalization at ingest.go:90 (fs.Put([]byte(in.Content), mime)).
        text, err := cfg.extractor.Extract(ctx, []byte(in.Content))
        if err != nil {
            return "", fmt.Errorf("extract pdf: %w", err)
        }
        pdfURI, _, err = s.fs.Put([]byte(in.Content), "application/pdf")
        if err != nil {
            return "", fmt.Errorf("store pdf blob: %w", err)
        }
        if text == "" {
            fmt.Fprintf(s.log,
                "warning: pdf extraction yielded no text (likely image-only or scanned); "+
                    "storing blob with empty content — search/embedding will not hit body text\n")
        }
        in.SourceMeta["original_uri"]  = pdfURI
        in.SourceMeta["original_mime"] = "application/pdf"
        in.Content = text
        in.MIME    = "text/plain"
    }

    // ... existing flow: build item, externalize text > 4KB, repo.Create ...

    // Extended rollback: on repo.Create failure, delete any blob we
    // created. The existing branch handles item.ContentURI (externalized
    // text); the new branch handles pdfURI (PDF blob). Both must be
    // cleaned or we leak orphaned refcount=1 blobs.
    if err := s.repo.Create(ctx, item); err != nil {
        if item.ContentURI != "" {
            _ = s.fs.Delete(item.ContentURI)
        }
        if pdfURI != "" {
            _ = s.fs.Delete(pdfURI)
        }
        return "", fmt.Errorf("persist item: %w", err)
    }

    // Embedding: when PDF extraction yielded empty text, item.Content
    // is "" and item.ContentURI is "" (no text externalization). Calling
    // Embed(title, "") would produce a title-only vector — misleading
    // for semantic search (the item has no body signal). Skip embed
    // with a warning in that specific case.
    //
    // IMPORTANT: scope the skip to the PDF path via pdfURI != "" so we
    // don't change behavior for non-PDF flows. A user may legitimately
    // `unictx user note add ""` (empty content) and the existing
    // contract calls embed unconditionally — preserve that.
    skipEmbed := pdfURI != "" && item.Content == "" && item.ContentURI == ""
    if s.embed != nil && !skipEmbed {
        if err := s.embed.Embed(ctx, item.ID, item.Title, item.Content); err != nil {
            fmt.Fprintf(s.log, "warn: embed failed for %s: %v\n", item.ID, err)
        }
    } else if s.embed != nil && skipEmbed {
        fmt.Fprintf(s.log, "warn: skipping embed for %s (empty extracted content)\n", item.ID)
    }

    // ... ReindexFTS, return item.ID ...
}
```

The rollback and embed-skip additions are the two places where the PDF
branch has to extend existing control flow rather than just transform
Input upfront.

## CLI surface

### File-size cap

`maxFileBytes` (user_note.go:308) bumps from `10 * 1024 * 1024` →
`50 * 1024 * 1024`. Text files rarely approach this; PDFs get
realistic headroom. **Test impact:** `checkFileSize` has pure-function
boundary tests in `user_note_test.go` (or equivalent); the at-cap and
cap+1 cases must move to the new value. The implementation plan must
list those tests and update their expected values.

### MIME detection — rename + extend

`mimeForTextFile` (user_note.go:314) becomes `mimeForFile` (the old
name lies once it handles PDFs). Update the call site at user_note.go:81
and any test that references the old name.

```go
// mimeForFile returns the MIME type for a path based on extension.
// Unknown extensions fall back to text/plain (backward compat).
func mimeForFile(path string) string {
    switch strings.ToLower(filepath.Ext(path)) {
    case ".md", ".markdown":
        return "text/markdown"
    case ".pdf":
        return "application/pdf"
    default:
        return "text/plain"
    }
}
```

### New `--engine` flag

```go
cmd.Flags().String("engine", "",
    `pdf extractor override: "gxpdf", "shell", or "http". `+
        `Empty uses the config default (pdf.engine).`)
```

Validation: any value not in `{"" | "gxpdf" | "shell" | "http"}` is
rejected before ingest.

### Runtime resolution order

1. Read `--engine` flag. If empty, fall back to `cfg.PDF.Engine` (where `cfg` is the `*config.Config` returned by `Load`).
2. If still empty AND MIME is `application/pdf` → friendly error
   pointing at config.
3. Otherwise build the extractor via
   `app.BuildExtractorForEngine(name, cfg.PDF)`.
4. Pass to `a.Ingest.Create(ctx, in, service.WithExtractor(ext))`.

## Config schema

`PDFConfig` and `EngineConfig` are added to `internal/config/config.go`
alongside the existing `EmbedderConfig`. They live in package `config`
because that's where all other YAML types live — putting them in `app`
would create a cycle (`app` → `config` for the rest of the types,
`config` → `app` for these). Add a `PDF PDFConfig` field to the
top-level `Config` struct.

```go
// in internal/config/config.go
type Config struct {
    User     UserConfig     `yaml:"user"`
    DataDir  string         `yaml:"data_dir"`
    Embedder EmbedderConfig `yaml:"embedder"`
    PDF      PDFConfig      `yaml:"pdf"`     // NEW
}

type PDFConfig struct {
    Engine  string                  `yaml:"engine"`  // gxpdf | shell | http; empty = disabled
    Engines map[string]EngineConfig `yaml:"engines"`
}

type EngineConfig struct {
    Command   string        `yaml:"command"`    // shell: "pdftotext - -"
    URL       string        `yaml:"url"`         // http: "http://localhost:8000/extract"
    Timeout   time.Duration `yaml:"timeout"`    // both; default 30s if zero
    AuthToken string        `yaml:"auth_token"` // http: Bearer token, optional
}
```

Defaults applied in `BuildExtractorForEngine`: zero `Timeout` → 30s.
Unlike `EmbedderConfig`, `PDFConfig` does NOT need a defaults pass in
`config.Load` because the only meaningful default (30s timeout) is
applied at the factory layer where the type is concrete. An empty
`Engine` field means "PDF support disabled," mirroring how
`EmbedderConfig.Enabled=false` works.

### Example config.yaml

```yaml
pdf:
  engine: gxpdf
  engines:
    shell:
      command: 'pdftotext - -'
      timeout: 30s
    http:
      url: 'http://localhost:8000/extract'
      timeout: 30s
```

### App-layer factory (`internal/app/pdf.go`)

```go
// BuildPDFExtractor returns the default extractor per cfg.Engine.
// Returns (nil, nil) when PDF is unconfigured — caller proceeds
// without PDF support, and the service errors clearly if a PDF
// is passed.
func BuildPDFExtractor(cfg config.PDFConfig, log io.Writer) (port.PDFExtractor, error)

// BuildExtractorForEngine returns an extractor for an explicit engine
// name. Used by CLI when --engine overrides the config default.
func BuildExtractorForEngine(name string, cfg config.PDFConfig, log io.Writer) (port.PDFExtractor, error)
```

Both funnel into a private `buildExtractor(name, engines, log)` switch
that constructs the adapter directly:

```go
switch name {
case "gxpdf":
    return pdf.NewGxpdfExtractor(log), nil       // log injected from app
case "shell":
    return pdf.NewShellExtractor(ec.Command, ec.Timeout), nil
case "http":
    return pdf.NewHttpExtractor(ec.URL, ec.Timeout, ec.AuthToken), nil
}
```

The `log` for `gxpdf` comes from the same `io.Writer` the app passes
to other services (currently `os.Stderr` at the Wire call site).
Missing config for the chosen engine (e.g., `shell` selected but
`pdf.engines.shell.command` empty) → error naming the specific config
key to set.

### Wiring in `app.Wire` (`internal/app/app.go`)

Add alongside the existing `embedder` wiring block. The existing Wire
has two branches (with and without embedder — Plan 1 compat); both
must receive the variadic opts so PDF support works regardless of
embedder configuration:

```go
// existing two-branch structure:
//   ingest := service.NewIngestService(repo, fs, os.Stderr)
//   if embedSvc != nil {
//       ingest = service.NewIngestServiceWithEmbedder(repo, fs, embedSvc, os.Stderr)
//   }

// new: build opts once, pass to both branches
pdfExt, err := BuildPDFExtractor(cfg.PDF, os.Stderr)
if err != nil {
    return nil, fmt.Errorf("build pdf extractor: %w", err)
}
opts := []service.IngestOption{}
if pdfExt != nil {
    opts = append(opts, service.WithPDFExtractor(pdfExt))
}
ingest := service.NewIngestService(repo, fs, os.Stderr, opts...)
if embedSvc != nil {
    ingest = service.NewIngestServiceWithEmbedder(repo, fs, embedSvc, os.Stderr, opts...)
}
```

The CLI's `--engine` override path goes through the same
`BuildExtractorForEngine` factory at call time and passes the result
via `service.WithExtractor(ext)` (a `CreateOption`). The constructor
variant is `WithPDFExtractor` (an `IngestOption`); the per-call
variant is `WithExtractor` (a `CreateOption`). Different names are
required because Go does not support function overloading.

## Error matrix

| Layer | Condition | Error surfaced |
|---|---|---|
| CLI flag parse | `--engine bogus` | `unknown pdf engine "bogus" (want gxpdf\|shell\|http)` |
| CLI flag parse | `--file` > 50MB | `file too large: <N> bytes (max 52428800)` |
| CLI runtime | `--engine shell` but config has no `pdf.engines.shell.command` | `engine 'shell' not configured (set pdf.engines.shell.command)` |
| CLI runtime | `--file paper.pdf`, no engine in config or flag | `pdf extraction not configured: set pdf.engine in config or pass --engine` |
| Service | MIME=pdf, extractor nil at call time | same message as above |
| Service | Extractor returns error | `extract pdf: <underlying>` |
| Service | Extractor returns empty | (not an error) — store blob, Content="", log warning |
| Service | `fs.Put(pdf bytes)` fails | `store pdf blob: <underlying>` |
| gxpdf adapter | Malformed PDF | wraps underlying parse error |
| gxpdf adapter | Encrypted PDF, no password | `encrypted pdf: password required` |
| gxpdf adapter | Per-page error | logs page index, continues, accumulates successful pages |
| shell adapter | Non-zero exit | `shell command failed (exit <N>): <stderr trimmed>` |
| shell adapter | Timeout | `shell command timed out after <dur>` (wraps `context.DeadlineExceeded`) |
| shell adapter | Binary not found | `shell command not found: <cmd>` |
| http adapter | Non-2xx | `http <status>: <body up to 256 bytes>` |
| http adapter | Timeout | `http request timed out after <dur>` |
| http adapter | Wrong response MIME | `unexpected response MIME <got>, want text/plain` |

## Testing strategy

### 1. Adapter unit tests (`internal/adapter/pdf/<engine>_test.go`)

- **gxpdf**: three committed PDF fixtures (see Fixtures below). One
  test per fixture for the three contracts (valid, image-only,
  encrypted).
- **shell**: temp shell scripts written by the test
  (`os.CreateTemp` + `chmod +x`) implementing `echo "fake text"`,
  `exit 1`, `sleep 5`. No reliance on production binaries.
- **http**: `httptest.Server` returning canned text/plain, 500, wrong
  MIME (text/html), and a slow handler to trigger timeout.

### 2. Service unit tests (`internal/service/ingest_test.go`)

Using a fake `PDFExtractor`:

| Test | Asserts |
|---|---|
| `Create_PDF_ExtractsAndStoresBlob` | Returns item ID non-empty; `fs.Put` called once with `application/pdf` MIME; `SourceMeta["original_uri"]` set |
| `Create_PDF_EmptyExtraction_StoresBlobEmptyContent` | Returns ID; item.Content = ""; SourceMeta still has `original_uri`; `s.log` contains `"warning: pdf extraction yielded no text"` AND `"skipping embed"`. **Must construct via `NewIngestServiceWithEmbedder` with a fake embedder** — the "skipping embed" branch only fires when `s.embed != nil && skipEmbed`. Without an embedder, `s.embed` is nil and the second log line never appears; the test would silently fail its assertion. |
| `Create_PDF_ErrorsWithoutExtractor` | MIME=pdf + no extractor → error matches `"pdf extraction not configured"`; returns `("", err)` |
| `Create_PDF_PropagatesExtractorError` | Extractor errors → wrapped as `"extract pdf: <orig>"`; returns `("", err)` |
| `Create_PDF_WithExtractorOverride` | Constructor has no extractor configured (pdfExtractor=nil), per-call override via `WithExtractor(ext)` supplies one; assert override wins (extractor is called, extractor's returned text is what lands in item) |
| `Create_PDF_LargeExtractedText_ExternalizesTextOnly` | Extracted text > 4KB → externalized to a *text* FileStore entry; ContentURI points to text blob; `SourceMeta["original_uri"]` still points to PDF blob (two distinct URIs — asymmetric on purpose) |
| `Create_PDF_RollsBackBothBlobsOnRepoFailure` | Stub repo to fail on Create; with extracted text > 4KB (so text was externalized) AND a PDF blob stored, assert `fs.Delete` called twice (once for text URI, once for pdfURI); returns `("", err)` |

The last two tests pin down load-bearing invariants:
- `LargeExtractedText_ExternalizesTextOnly`: large extracted text does
  NOT overwrite `original_uri` with the text blob's URI.
- `RollsBackBothBlobsOnRepoFailure`: the rollback extension actually
  fires for the PDF blob, not just the text blob. Without this test,
  a future refactor could silently drop the `pdfURI != ""` branch and
  leak orphans on every repo.Create failure.

### 3. App factory tests (`internal/app/pdf_test.go`)

- `BuildPDFExtractor_DefaultsToGxpdf` — engine=gxpdf returns non-nil
- `BuildPDFExtractor_NilWhenUnconfigured` — engine="" returns `(nil, nil)`
- `BuildExtractorForEngine_ErrorsOnUnknownName`
- `BuildExtractorForEngine_ErrorsOnMissingShellConfig`
- `BuildExtractorForEngine_ErrorsOnMissingHTTPConfig`

### 4. CLI integration tests (`internal/cli/user_note_run_e_test.go`)

- `Add_PDF_NoEngineNoConfig_Errors` — friendly message
- `Add_PDF_UnknownEngineValue_Errors`
- `Add_PDF_PassesExtractorOverride` — configure `pdf.engine=shell`
  with a stub shell command (script that prints canned text via
  stdout); run `user note add --file sample.pdf`; retrieve the
  created item via `Items.Get`; assert `item.Content` equals the
  canned text the stub script printed. This verifies the override
  plumbing end-to-end (flag → factory → service → repo) without
  needing to inspect private `createConfig` state. (Testing the
  gxpdf path here would also work but couples CLI tests to the
  gxpdf dep; the shell path uses a stub script the test owns.)
  **Fixture wiring:** the test goes through `swapUserNoteLoadAppFn`
  (or equivalent pattern in `user_note_run_e_test.go`); the returned
  `*config.Config` must set `PDF.Engine = "shell"` AND
  `PDF.Engines["shell"].Command` to the absolute path of the temp
  script the test created via `os.CreateTemp` + `os.Chmod(0o755)`.
  Without `Engines["shell"].Command` populated, `BuildExtractorForEngine`
  errors with "engine 'shell' not configured" before reaching the
  service.
- `Add_PDF_SizeCapIsFiftyMB` — fixture file at 50 MB boundary passes;
  50 MB + 1 byte fails. Replaces any prior 10 MB boundary test.

A full end-to-end (real PDF → gxpdf → real DB) is skipped: components
are individually covered, and an e2e would couple CLI tests to the
gxpdf dependency without adding signal beyond what
`Add_PDF_PassesExtractorOverride` already provides.

## Fixtures (`internal/adapter/pdf/testdata/`)

- `sample.pdf` — small valid PDF (~5KB) with phrase
  `"the quick brown fox"` in body.
- `blank.pdf` — single blank page, no text layer.
- `encrypted.pdf` — encrypted with a password the tests do not
  provide (so the "encrypted" error path is exercised).

Fixtures are committed to the repo. Generated once during
implementation; total footprint < 20KB. Stability is high (binary
PDFs don't drift).

## Future work (explicitly deferred)

- `--password <str>` for encrypted PDFs (gxpdf supports it via
  `OpenWithPassword`).
- `--pages 1-10` for page-range extraction.
- Generalized `DocumentExtractor` port covering docx/html/images-with-OCR
  via the same engine-selection surface.
- Dedup: skip ingest if a PDF with the same sha256 is already stored
  (would surface via `SourceMeta["original_uri"]` lookup).
- `--no-size-limit` for files > 50MB.
