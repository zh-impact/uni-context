# PDF Attach for `user note add` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `unictx user note add --file paper.pdf` produces a searchable, embeddable context item backed by both the extracted text and the original PDF blob.

**Architecture:** Add a `port.PDFExtractor` interface with three adapters (gxpdf/shell/http); inject one into `IngestService` via constructor option (`WithPDFExtractor`); CLI passes a per-call override via `WithExtractor` when `--engine` is set. Original PDF bytes are stored in `FileStore` with the URI captured in `SourceMeta["original_uri"]`; extracted text flows through the existing externalize/repo/FTS/embed pipeline unchanged.

**Tech Stack:** Go 1.25, `github.com/coregx/gxpdf` (pure-Go PDF text extraction), cobra CLI, yaml.v3 config, httptest for HTTP adapter tests, testify for assertions.

## Global Constraints

- **Go version:** 1.25 (project floor; gxpdf requires it).
- **Build/test command:** `HTTPS_PROXY=socks5://127.0.0.1:7890 CGO_ENABLED=1 go test -tags 'sqlite_fts5' -count=1 ./...` — proxy required for `go get`, FTS5 tag required for SQLite tests.
- **Format on edit:** run `goimports -w` on every `.go` file touched (matches VSCode format-on-save).
- **Return type discipline:** `IngestService.Create` returns `(string, error)` — only the item ID, not the full `domain.ContextItem`. All new error returns from the PDF branch must be `return "", err`.
- **Naming:** `WithPDFExtractor` is the `IngestOption` (constructor); `WithExtractor` is the `CreateOption` (per-call). Different names are required because Go does not support function overloading.
- **Rollback contract:** on `repo.Create` failure, BOTH `item.ContentURI` (externalized text) AND `pdfURI` (PDF blob, captured in the PDF branch) must be `fs.Delete`'d. Existing rollback at `ingest.go:111-113` only handles the former.
- **Embed-skip scope:** only skip embed when `pdfURI != "" && item.Content == "" && item.ContentURI == ""` (image-only PDF case). Do NOT change embed behavior for non-PDF empty-content items (existing contract).
- **TDD cycle per task:** failing test → implement → green → commit. No exceptions.
- **Commit message prefix:** use `feat(pdf):`, `refactor(pdf):`, `test(pdf):`, `docs(pdf):` as appropriate.
- **gxpdf version:** `github.com/coregx/gxpdf` latest as of implementation. Pin in go.mod.

---

## File Structure

```
internal/port/pdf.go                       [NEW] PDFExtractor interface
internal/adapter/pdf/
├── gxpdf.go                              [NEW] GxpdfExtractor (default engine)
├── gxpdf_test.go                         [NEW] Tests using testdata/
├── shell.go                              [NEW] ShellExtractor (exec.Command)
├── shell_test.go                         [NEW] Tests via temp scripts
├── http.go                               [NEW] HttpExtractor (POST binary)
├── http_test.go                          [NEW] Tests via httptest.Server
└── testdata/                             [NEW] Committed PDF fixtures
    ├── sample.pdf                          (~5KB, contains "the quick brown fox")
    ├── blank.pdf                           (single blank page, no text layer)
    ├── encrypted.pdf                       (encrypted; tests do not provide password)
    └── README.md                           [NEW] How fixtures were generated
internal/config/config.go                  [MODIFY] Add PDFConfig, EngineConfig types + Config.PDF field
internal/config/config_test.go             [MODIFY] Add YAML round-trip test for PDFConfig
internal/app/pdf.go                        [NEW] BuildPDFExtractor, BuildExtractorForEngine factories
internal/app/pdf_test.go                   [NEW] Factory tests
internal/app/app.go                        [MODIFY] Wire: build extractor, pass via opts
internal/service/ingest.go                 [MODIFY] Add pdfExtractor field, IngestOption, CreateOption, PDF branch, rollback extension, embed-skip
internal/service/ingest_test.go            [MODIFY] Add 7 PDF branch tests
internal/cli/user_note.go                  [MODIFY] Rename mimeForTextFile → mimeForFile, accept .pdf, bump maxFileBytes, add --engine flag
internal/cli/user_note_test.go             [MODIFY] Update mimeFor* test names; bump size cap tests
internal/cli/user_note_run_e_test.go       [MODIFY] Add PDF CLI integration tests
```

---

## Task 1: PDFConfig + EngineConfig types in `internal/config/config.go`

**Why first:** has no dependencies on new code; later tasks (factory, wiring) consume these types. Independent of adapter work — can land before or in parallel with Task 2.

**Files:**
- Modify: `internal/config/config.go` (add types + field to Config struct)
- Modify: `internal/config/config_test.go` (add YAML round-trip test)

**Interfaces:**
- Produces: `config.PDFConfig`, `config.EngineConfig` types; `Config.PDF` field. Used by Task 5 (factory).

- [ ] **Step 1: Write the failing test**

Add to `internal/config/config_test.go`:

```go
func TestLoad_PDFConfig(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	yaml := []byte(`
pdf:
  engine: shell
  engines:
    shell:
      command: 'pdftotext - -'
      timeout: 30s
    http:
      url: 'http://localhost:8000/extract'
      timeout: 45s
      auth_token: 'secret'
`)
	require.NoError(t, os.WriteFile(path, yaml, 0o600))
	cfg, err := config.Load(path)
	require.NoError(t, err)
	require.Equal(t, "shell", cfg.PDF.Engine)
	require.Len(t, cfg.PDF.Engines, 2)

	shell := cfg.PDF.Engines["shell"]
	assert.Equal(t, "pdftotext - -", shell.Command)
	assert.Equal(t, 30*time.Second, shell.Timeout)

	http := cfg.PDF.Engines["http"]
	assert.Equal(t, "http://localhost:8000/extract", http.URL)
	assert.Equal(t, 45*time.Second, http.Timeout)
	assert.Equal(t, "secret", http.AuthToken)
}

func TestLoad_PDFConfig_EmptyByDefault(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	require.NoError(t, os.WriteFile(path, []byte(`user: { id: test }`), 0o600))
	cfg, err := config.Load(path)
	require.NoError(t, err)
	assert.Equal(t, "", cfg.PDF.Engine, "PDF engine defaults to empty (disabled)")
	assert.Nil(t, cfg.PDF.Engines, "PDF engines map defaults to nil")
}
```

Add imports (`os`, `path/filepath`, `time`) if not already present.

- [ ] **Step 2: Run test to verify it fails**

```
HTTPS_PROXY=socks5://127.0.0.1:7890 go test ./internal/config/ -run TestLoad_PDFConfig -v
```

Expected: FAIL with compile error `cfg.PDF undefined`.

- [ ] **Step 3: Implement minimal types**

In `internal/config/config.go`, add the types after `EmbedderConfig`:

```go
// PDFConfig controls the optional PDF text-extraction pipeline. When
// Engine is empty (the default), PDF support is disabled: passing
// Input with MIME "application/pdf" to IngestService.Create returns
// a clear "pdf extraction not configured" error. When Engine is set
// ("gxpdf", "shell", or "http"), app.Wire constructs the matching
// adapter and injects it into IngestService.
//
// Unlike EmbedderConfig, no defaults pass runs in Load — the only
// meaningful default (30s timeout) is applied at the factory layer
// where the engine type is concrete.
type PDFConfig struct {
	Engine  string                  `yaml:"engine"`  // gxpdf | shell | http; empty = disabled
	Engines map[string]EngineConfig `yaml:"engines"`
}

// EngineConfig holds engine-specific fields. Only the fields relevant
// to the chosen engine are populated; others stay zero-valued.
type EngineConfig struct {
	Command   string        `yaml:"command"`    // shell: "pdftotext - -"
	URL       string        `yaml:"url"`         // http: "http://localhost:8000/extract"
	Timeout   time.Duration `yaml:"timeout"`    // both; zero → 30s at factory time
	AuthToken string        `yaml:"auth_token"` // http: sent as Authorization: Bearer
}
```

Add the field to the top-level Config struct:

```go
type Config struct {
	User     UserConfig     `yaml:"user"`
	DataDir  string         `yaml:"data_dir"`
	Embedder EmbedderConfig `yaml:"embedder"`
	PDF      PDFConfig      `yaml:"pdf"`
}
```

Add `"time"` to the import block if not present.

- [ ] **Step 4: Run tests to verify pass**

```
HTTPS_PROXY=socks5://127.0.0.1:7890 go test ./internal/config/ -v
```

Expected: PASS, all tests including the new ones.

- [ ] **Step 5: Format and commit**

```
goimports -w internal/config/config.go internal/config/config_test.go
git add internal/config/config.go internal/config/config_test.go
git commit -m "feat(config): add PDFConfig and EngineConfig types

PDF block in config.yaml controls text extraction:
  pdf:
    engine: gxpdf  # or shell | http
    engines:
      shell: { command: 'pdftotext - -', timeout: 30s }
      http:  { url: '...', timeout: 30s, auth_token: '...' }

Empty Engine means PDF support disabled. No defaults applied at Load;
factory applies the 30s timeout default when constructing adapters."
```

---

## Task 2: port.PDFExtractor interface + GxpdfExtractor + test fixtures

**Files:**
- Create: `internal/port/pdf.go`
- Create: `internal/adapter/pdf/gxpdf.go`
- Create: `internal/adapter/pdf/gxpdf_test.go`
- Create: `internal/adapter/pdf/testdata/sample.pdf`
- Create: `internal/adapter/pdf/testdata/blank.pdf`
- Create: `internal/adapter/pdf/testdata/encrypted.pdf`
- Create: `internal/adapter/pdf/testdata/README.md`
- Modify: `go.mod` (add `github.com/coregx/gxpdf`)

**Interfaces:**
- Produces: `port.PDFExtractor` interface, `pdf.NewGxpdfExtractor(log io.Writer) *GxpdfExtractor`. Used by Task 5 (factory), Task 6 (service tests via fake).

- [ ] **Step 1: Add gxpdf dependency and verify API surface**

```
HTTPS_PROXY=socks5://127.0.0.1:7890 go get github.com/coregx/gxpdf@latest
```

Verify `go.mod` has the new require line.

**Then verify the API surface the implementation assumes.** The spec confirmed three methods (`NewReader`, `doc.Pages()`, `page.ExtractText()`); the implementation also calls `defer doc.Close()`. Confirm `Close()` exists before writing Step 6's code:

```
HTTPS_PROXY=socks5://127.0.0.1:7890 go doc github.com/coregx/gxpdf | grep -i close
```

Or grep the module sources directly:

```
grep -rn 'func.*Close' "$(go env GOMODCACHE)/github.com/coregx/gxpdf@"*/
```

If `*gxpdf.Document` has no `Close()` method (or it's named differently — `Release`, `Free`, etc.), adjust Step 6's implementation accordingly. If no cleanup method exists, drop the `defer doc.Close()` line entirely — pure-Go PDF readers typically don't hold OS resources that need explicit release, so leak risk is minimal within a single function scope.

- [ ] **Step 2: Create test fixtures**

Each fixture is small (<10KB). Generate using any method you prefer (pdfcpu CLI, manual, etc.) and commit the binaries. Required contracts:

- `sample.pdf`: a 1-page PDF whose body text contains the phrase `"the quick brown fox"`. ~5KB.
- `blank.pdf`: a 1-page PDF with a blank page (no text drawn). gxpdf's `ExtractText()` on this file returns `""`.
- `encrypted.pdf`: a PDF encrypted with a password. Opening without a password fails; gxpdf returns an error containing `"encrypted"`.

Verify each fixture locally:

```
pdftotext sample.pdf - | grep "the quick brown fox"   # should print the phrase
pdftotext blank.pdf -                                 # should print nothing
qpdf --requires-password encrypted.pdf                # should exit 0 (yes, encrypted)
```

If `pdftotext`/`qpdf` are unavailable, the unit tests in Step 5 are the source of truth.

Create `internal/adapter/pdf/testdata/README.md` documenting how the fixtures were generated (which tool/version) so they can be regenerated if needed. Example:

```markdown
# PDF test fixtures

Generated 2026-06-26 via:
- sample.pdf: `printf '%s' '%PDF-1.4...' > sample.pdf` (hand-crafted minimal PDF)
- blank.pdf: same structure as sample.pdf with empty content stream
- encrypted.pdf: `qpdf -encrypt user owner 256 - sample.pdf encrypted.pdf`

Contracts verified by gxpdf_test.go.
```

- [ ] **Step 3: Write the failing test**

Create `internal/adapter/pdf/gxpdf_test.go`:

```go
package pdf

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// fixturePath resolves testdata/<name> relative to the test file.
// Tests run with cwd = package dir, so the path is just "testdata/<name>".
func fixturePath(t *testing.T, name string) string {
	t.Helper()
	p := filepath.Join("testdata", name)
	if _, err := os.Stat(p); err != nil {
		t.Fatalf("fixture %s missing: %v (regenerate per testdata/README.md)", p, err)
	}
	return p
}

func TestGxpdfExtractor_ExtractsKnownText(t *testing.T) {
	data, err := os.ReadFile(fixturePath(t, "sample.pdf"))
	require.NoError(t, err)
	x := NewGxpdfExtractor(&bytes.Buffer{})
	text, err := x.Extract(t.Context(), data)
	require.NoError(t, err)
	assert.Contains(t, text, "the quick brown fox",
		"extracted text must contain the body phrase")
}

func TestGxpdfExtractor_EmptyExtractionIsNotError(t *testing.T) {
	// Contract: image-only / blank PDFs return ("", nil), NOT an error.
	// The IngestService relies on this to distinguish "no text" from
	// "broken PDF" — the former stores the blob with empty Content,
	// the latter fails the entire Create call.
	data, err := os.ReadFile(fixturePath(t, "blank.pdf"))
	require.NoError(t, err)
	x := NewGxpdfExtractor(&bytes.Buffer{})
	text, err := x.Extract(t.Context(), data)
	require.NoError(t, err, "empty extraction must NOT be an error")
	assert.Empty(t, text, "blank PDF should yield no extractable text")
}

func TestGxpdfExtractor_EncryptedReturnsError(t *testing.T) {
	data, err := os.ReadFile(fixturePath(t, "encrypted.pdf"))
	require.NoError(t, err)
	x := NewGxpdfExtractor(&bytes.Buffer{})
	_, err = x.Extract(t.Context(), data)
	require.Error(t, err, "encrypted PDF without password must error")
	assert.Contains(t, err.Error(), "encrypted",
		"error message must include 'encrypted' so callers can message clearly")
}

func TestGxpdfExtractor_MalformedPDFReturnsError(t *testing.T) {
	x := NewGxpdfExtractor(&bytes.Buffer{})
	_, err := x.Extract(t.Context(), []byte("not a pdf at all"))
	require.Error(t, err)
}

// Verify the testdata dir is visible to go test (embedded FS sanity).
func TestTestDataFixturesExist(t *testing.T) {
	for _, name := range []string{"sample.pdf", "blank.pdf", "encrypted.pdf"} {
		_, err := os.ReadFile(filepath.Join("testdata", name))
		require.NoError(t, err, "fixture %s missing; see testdata/README.md", name)
	}
}
```

- [ ] **Step 4: Run test to verify it fails (compile error)**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/pdf/ -v
```

Expected: FAIL with `pdfExtractor undefined` / `NewGxpdfExtractor undefined`.

- [ ] **Step 5: Create the port interface**

Create `internal/port/pdf.go`:

```go
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

- [ ] **Step 6: Implement GxpdfExtractor**

Create `internal/adapter/pdf/gxpdf.go`:

```go
// Package pdf provides adapters for the port.PDFExtractor interface.
// Three engines are supported: gxpdf (pure Go, default), shell
// (subprocess such as pdftotext), and http (POST binary to a service).
package pdf

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"strings"

	"github.com/coregx/gxpdf"

	"uni-context/internal/port"
)

// GxpdfExtractor wraps github.com/coregx/gxpdf. Pure Go, no external
// deps; the default engine when none is configured.
type GxpdfExtractor struct {
	log io.Writer
}

// NewGxpdfExtractor constructs an extractor that logs per-page errors
// (non-fatal — partial extraction is more useful than total failure)
// to log. Pass io.Discard in production if you want silent operation,
// or a *bytes.Buffer in tests to assert on warnings.
func NewGxpdfExtractor(log io.Writer) *GxpdfExtractor {
	return &GxpdfExtractor{log: log}
}

// Compile-time interface check.
var _ port.PDFExtractor = (*GxpdfExtractor)(nil)

// Extract reads content as a PDF and returns the concatenated text of
// all pages. Per-page errors are logged and skipped; the remaining
// pages still contribute their text. Encrypted PDFs (no password
// supplied) return an error containing "encrypted".
func (x *GxpdfExtractor) Extract(ctx context.Context, content []byte) (string, error) {
	// gxpdf's API: NewReader takes an io.ReaderAt + size. Use bytes.Reader.
	r := bytes.NewReader(content)
	doc, err := gxpdf.NewReader(r, int64(len(content)))
	if err != nil {
		// Distinguish encrypted-PDF errors from generic parse errors so
		// callers can surface "password required" specifically.
		if isEncryptedErr(err) {
			return "", fmt.Errorf("encrypted pdf: password required: %w", err)
		}
		return "", fmt.Errorf("open pdf: %w", err)
	}
	defer doc.Close()

	pages := doc.Pages()
	var b strings.Builder
	for i, page := range pages {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		default:
		}
		text, perr := page.ExtractText()
		if perr != nil {
			// Non-fatal: log and continue with the pages that did work.
			fmt.Fprintf(x.log, "warn: gxpdf page %d: %v\n", i, perr)
			continue
		}
		b.WriteString(text)
		if i < len(pages)-1 {
			b.WriteString("\n")
		}
	}
	return b.String(), nil
}

// isEncryptedErr heuristically detects gxpdf's encryption error.
// The library returns errors whose message contains "encrypted" when
// the PDF requires a password; we match on substring to surface a
// clear message without coupling to the exact error type.
func isEncryptedErr(err error) bool {
	return err != nil && strings.Contains(strings.ToLower(err.Error()), "encrypt")
}
```

(The `"strings"` import is already in the import block above — `strings.Builder` is used in the page loop and `strings.Contains`/`ToLower` in `isEncryptedErr`.)

- [ ] **Step 7: Run tests to verify pass**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/adapter/pdf/ -v
```

Expected: all four tests PASS.

If `TestGxpdfExtractor_EncryptedReturnsError` fails because gxpdf's actual error message doesn’t contain `"encrypted"`, inspect the error text (`t.Logf("%v", err)`) and adjust `isEncryptedErr` accordingly. Document the actual message in the test comment.

- [ ] **Step 8: Format and commit**

```
goimports -w internal/port/pdf.go internal/adapter/pdf/gxpdf.go internal/adapter/pdf/gxpdf_test.go
git add internal/port/pdf.go internal/adapter/pdf/ go.mod go.sum
git commit -m "feat(pdf): add port.PDFExtractor and GxpdfExtractor

port.PDFExtractor defines the contract for PDF → text extraction.
Empty extraction returns (\"\", nil) — not an error — so callers can
distinguish 'image-only PDF' from 'broken PDF'.

GxpdfExtractor wraps github.com/coregx/gxpdf (pure Go, MIT, Go 1.25+).
Per-page errors are logged and skipped; remaining pages still contribute.
Encrypted PDFs return a clear error mentioning 'encrypted'.

Three test fixtures committed under testdata/: sample (valid text),
blank (image-only), encrypted (password required)."
```

---

## Task 3: ShellExtractor adapter

**Files:**
- Create: `internal/adapter/pdf/shell.go`
- Create: `internal/adapter/pdf/shell_test.go`

**Interfaces:**
- Consumes: `port.PDFExtractor` (from Task 2).
- Produces: `pdf.NewShellExtractor(command string, timeout time.Duration) *ShellExtractor`.

- [ ] **Step 1: Write the failing test**

Create `internal/adapter/pdf/shell_test.go`:

```go
package pdf

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// writeScript creates an executable temp file whose body is script.
// Returns the absolute path. Skips the test on OSes where chmod +x
// isn't meaningful (Windows).
func writeScript(t *testing.T, script string) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("shell extractor tests rely on chmod +x; skip on Windows")
	}
	f, err := os.CreateTemp("", "shell-ext-*")
	require.NoError(t, err)
	defer f.Close()
	_, err = f.WriteString(script)
	require.NoError(t, err)
	require.NoError(t, f.Chmod(0o755))
	abs, err := filepath.Abs(f.Name())
	require.NoError(t, err)
	return abs
}

func TestShellExtractor_ExtractsStdout(t *testing.T) {
	// Stub script that prints canned text to stdout and exits 0.
	stub := writeScript(t, "#!/bin/sh\necho 'canned extracted text'\n")
	x := NewShellExtractor(stub, 5*time.Second)

	text, err := x.Extract(context.Background(), []byte("%PDF-1.4 fake"))
	require.NoError(t, err)
	assert.Equal(t, strings.TrimSpace(text), "canned extracted text")
}

func TestShellExtractor_PropagatesNonZeroExit(t *testing.T) {
	stub := writeScript(t, "#!/bin/sh\necho 'parse failed' 1>&2\nexit 3\n")
	x := NewShellExtractor(stub, 5*time.Second)

	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	s := err.Error()
	assert.Contains(t, s, "exit 3", "error must mention exit code")
	assert.Contains(t, s, "parse failed", "error must include stderr")
}

func TestShellExtractor_TimesOut(t *testing.T) {
	stub := writeScript(t, "#!/bin/sh\nsleep 5\n")
	x := NewShellExtractor(stub, 100*time.Millisecond)

	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "timeout", "error must mention timeout")
}

func TestShellExtractor_BinaryNotFound(t *testing.T) {
	x := NewShellExtractor("/nonexistent/path/definitely-not-here", 5*time.Second)
	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "not found",
		"error must mention the binary is missing")
}
```

- [ ] **Step 2: Run test to verify it fails**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestShellExtractor ./internal/adapter/pdf/ -v
```

Expected: FAIL with `NewShellExtractor undefined`.

- [ ] **Step 3: Implement ShellExtractor**

Create `internal/adapter/pdf/shell.go`:

```go
package pdf

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
	"time"

	"uni-context/internal/port"
)

// ShellExtractor spawns an external command, writes the PDF bytes to
// its stdin, and reads extracted text from stdout. The expected usage
// is a command like `pdftotext - -` (stdin → stdout).
//
// No shell interpretation: command is split via strings.Fields, so
// pipes, redirects, and globs do NOT work. Users who need them wrap
// their pipeline in a script and point command at the script path.
type ShellExtractor struct {
	command string
	timeout time.Duration
}

// NewShellExtractor constructs an extractor that runs command per
// call. If timeout is zero, the factory layer (BuildExtractorForEngine)
// replaces it with 30s before calling this constructor — but
// defensive code in Extract also clamps zero to 30s.
func NewShellExtractor(command string, timeout time.Duration) *ShellExtractor {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return &ShellExtractor{command: command, timeout: timeout}
}

var _ port.PDFExtractor = (*ShellExtractor)(nil)

func (x *ShellExtractor) Extract(ctx context.Context, content []byte) (string, error) {
	parts := strings.Fields(x.command)
	if len(parts) == 0 {
		return "", fmt.Errorf("shell extractor: empty command")
	}
	cmdName := parts[0]
	args := parts[1:]

	ctx, cancel := context.WithTimeout(ctx, x.timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, cmdName, args...)
	cmd.Stdin = bytes.NewReader(content)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		// Distinguish timeout from generic exit-non-zero.
		if ctx.Err() == context.DeadlineExceeded {
			return "", fmt.Errorf("shell command timed out after %s: %w",
				x.timeout, ctx.Err())
		}
		// Process never started: binary not found, not executable, or
		// permission denied. exec.CommandContext returns *os.PathError
		// for absolute paths that don't exist and *exec.Error for PATH
		// misses — checking either type explicitly misses the other.
		// cmd.ProcessState == nil is the reliable signal: if the process
		// never started, there's no ProcessState to read.
		if cmd.ProcessState == nil {
			return "", fmt.Errorf("shell command not found or not executable: %s: %w", cmdName, err)
		}
		// Exit non-zero: include exit code + stderr snippet.
		stderrSnippet := strings.TrimSpace(stderr.String())
		if len(stderrSnippet) > 256 {
			stderrSnippet = stderrSnippet[:256] + "..."
		}
		return "", fmt.Errorf("shell command failed (exit %v): %s",
			cmd.ProcessState.ExitCode(), stderrSnippet)
	}
	return stdout.String(), nil
}
```

- [ ] **Step 4: Run tests to verify pass**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestShellExtractor ./internal/adapter/pdf/ -v
```

Expected: all four tests PASS.

- [ ] **Step 5: Format and commit**

```
goimports -w internal/adapter/pdf/shell.go internal/adapter/pdf/shell_test.go
git add internal/adapter/pdf/shell.go internal/adapter/pdf/shell_test.go
git commit -m "feat(pdf): add ShellExtractor for external-command extraction

Runs an arbitrary subprocess (e.g. pdftotext), writes PDF bytes to
stdin, reads extracted text from stdout. Empty timeout defaults to
30s. Errors distinguish timeout vs. non-zero exit vs. binary missing.
Command is split via strings.Fields — no shell features (pipes/
redirects). Users needing them wrap their pipeline in a script."
```

---

## Task 4: HttpExtractor adapter

**Files:**
- Create: `internal/adapter/pdf/http.go`
- Create: `internal/adapter/pdf/http_test.go`

**Interfaces:**
- Consumes: `port.PDFExtractor`.
- Produces: `pdf.NewHttpExtractor(url string, timeout time.Duration, authToken string) *HttpExtractor`.

- [ ] **Step 1: Write the failing test**

Create `internal/adapter/pdf/http_test.go`:

```go
package pdf

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestHttpExtractor_POSTsBinaryAndReturnsTextBody(t *testing.T) {
	var (
		gotPath       string
		gotMethod     string
		gotContent    string
		gotAuth       string
		gotBodyBytes  []byte
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotMethod = r.Method
		gotContent = r.Header.Get("Content-Type")
		gotAuth = r.Header.Get("Authorization")
		gotBodyBytes = make([]byte, 1024)
		n, _ := r.Body.Read(gotBodyBytes)
		gotBodyBytes = gotBodyBytes[:n]
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = w.Write([]byte("server extracted this text"))
	}))
	defer srv.Close()

	x := NewHttpExtractor(srv.URL+"/extract", 5*time.Second, "tok-abc")
	text, err := x.Extract(context.Background(), []byte("%PDF-1.4 fake bytes"))
	require.NoError(t, err)
	assert.Equal(t, "server extracted this text", text)

	assert.Equal(t, "/extract", gotPath)
	assert.Equal(t, http.MethodPost, gotMethod)
	assert.Equal(t, "application/pdf", gotContent)
	assert.Equal(t, "Bearer tok-abc", gotAuth)
	assert.Equal(t, "%PDF-1.4 fake bytes", string(gotBodyBytes))
}

func TestHttpExtractor_ErrorsOnNon2xx(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte("malformed pdf body"))
	}))
	defer srv.Close()

	x := NewHttpExtractor(srv.URL, 5*time.Second, "")
	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	s := err.Error()
	assert.Contains(t, s, "422", "error must mention status code")
	assert.Contains(t, s, "malformed pdf body", "error must include body snippet")
}

func TestHttpExtractor_ErrorsOnWrongResponseMIME(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"text":"hi"}`))
	}))
	defer srv.Close()

	x := NewHttpExtractor(srv.URL, 5*time.Second, "")
	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "text/plain",
		"error must mention expected MIME")
}

func TestHttpExtractor_TimesOut(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(2 * time.Second)
	}))
	defer srv.Close()

	x := NewHttpExtractor(srv.URL, 100*time.Millisecond, "")
	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "timeout")
}

func TestHttpExtractor_OmitsAuthHeaderWhenTokenEmpty(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "text/plain")
	}))
	defer srv.Close()

	x := NewHttpExtractor(srv.URL, 5*time.Second, "")
	_, _ = x.Extract(context.Background(), []byte("fake"))
	assert.Empty(t, gotAuth, "no Authorization header when token empty")
}
```

- [ ] **Step 2: Run test to verify it fails**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestHttpExtractor ./internal/adapter/pdf/ -v
```

Expected: FAIL with `NewHttpExtractor undefined`.

- [ ] **Step 3: Implement HttpExtractor**

Create `internal/adapter/pdf/http.go`:

```go
package pdf

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"uni-context/internal/port"
)

// HttpExtractor POSTs the PDF bytes to a configured URL and reads
// the response body as plain text. Expected response Content-Type is
// text/plain (any charset); other MIMEs return an error.
type HttpExtractor struct {
	url     string
	timeout time.Duration
	authToken string
}

func NewHttpExtractor(url string, timeout time.Duration, authToken string) *HttpExtractor {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return &HttpExtractor{url: url, timeout: timeout, authToken: authToken}
}

var _ port.PDFExtractor = (*HttpExtractor)(nil)

func (x *HttpExtractor) Extract(ctx context.Context, content []byte) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, x.timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, x.url, bytes.NewReader(content))
	if err != nil {
		return "", fmt.Errorf("build http request: %w", err)
	}
	req.Header.Set("Content-Type", "application/pdf")
	if x.authToken != "" {
		req.Header.Set("Authorization", "Bearer "+x.authToken)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return "", fmt.Errorf("http request timed out after %s: %w", x.timeout, ctx.Err())
		}
		return "", fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
		return "", fmt.Errorf("http %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	ct := resp.Header.Get("Content-Type")
	if !strings.HasPrefix(strings.ToLower(ct), "text/plain") {
		return "", fmt.Errorf("unexpected response MIME %q, want text/plain", ct)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read response: %w", err)
	}
	return string(body), nil
}
```

- [ ] **Step 4: Run tests to verify pass**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestHttpExtractor ./internal/adapter/pdf/ -v
```

Expected: all five tests PASS.

- [ ] **Step 5: Format and commit**

```
goimports -w internal/adapter/pdf/http.go internal/adapter/pdf/http_test.go
git add internal/adapter/pdf/http.go internal/adapter/pdf/http_test.go
git commit -m "feat(pdf): add HttpExtractor for service-based extraction

POSTs PDF bytes (Content-Type: application/pdf) to a URL and reads
the response body as text/plain. Optional Bearer token for auth.
Errors distinguish: timeout, non-2xx (with body snippet up to 256b),
wrong response MIME. Default timeout 30s when zero."
```

---

## Task 5: App-layer factory (`internal/app/pdf.go`)

**Why now:** depends on Tasks 1-4 (adapters + config). Produces the entry point both Wire (Task 7) and CLI (Task 8) consume.

**Files:**
- Create: `internal/app/pdf.go`
- Create: `internal/app/pdf_test.go`

**Interfaces:**
- Consumes: `config.PDFConfig`, `config.EngineConfig` (Task 1); `pdf.NewGxpdfExtractor`/`NewShellExtractor`/`NewHttpExtractor` (Tasks 2-4).
- Produces: `app.BuildPDFExtractor(cfg config.PDFConfig, log io.Writer) (port.PDFExtractor, error)` and `app.BuildExtractorForEngine(name string, cfg config.PDFConfig, log io.Writer) (port.PDFExtractor, error)`.

- [ ] **Step 1: Write the failing test**

Create `internal/app/pdf_test.go`:

```go
package app

import (
	"bytes"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/config"
	"uni-context/internal/port"
)

func TestBuildPDFExtractor_NilWhenUnconfigured(t *testing.T) {
	ext, err := BuildPDFExtractor(config.PDFConfig{}, &bytes.Buffer{})
	require.NoError(t, err)
	assert.Nil(t, ext, "empty Engine means PDF disabled → (nil, nil)")
}

func TestBuildPDFExtractor_DefaultsToGxpdf(t *testing.T) {
	ext, err := BuildPDFExtractor(config.PDFConfig{Engine: "gxpdf"}, &bytes.Buffer{})
	require.NoError(t, err)
	require.NotNil(t, ext)
	// Don't assert concrete type — that couples the test to the impl.
	// Just verify it satisfies the port (the compile-time var in the
	// impl already does this, but a runtime check here is defensive).
	var _ port.PDFExtractor = ext
}

func TestBuildExtractorForEngine_ErrorsOnUnknownName(t *testing.T) {
	_, err := BuildExtractorForEngine("bogus", config.PDFConfig{}, &bytes.Buffer{})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "unknown pdf engine")
	assert.Contains(t, err.Error(), "bogus")
}

func TestBuildExtractorForEngine_ErrorsOnMissingShellConfig(t *testing.T) {
	// engine=shell but Engines map is nil → no command.
	_, err := BuildExtractorForEngine("shell", config.PDFConfig{}, &bytes.Buffer{})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "shell")
	assert.Contains(t, err.Error(), "command")
}

func TestBuildExtractorForEngine_ErrorsOnMissingHTTPConfig(t *testing.T) {
	_, err := BuildExtractorForEngine("http", config.PDFConfig{}, &bytes.Buffer{})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "http")
	assert.Contains(t, err.Error(), "url")
}

func TestBuildExtractorForEngine_ShellAppliesTimeoutDefault(t *testing.T) {
	cfg := config.PDFConfig{
		Engines: map[string]config.EngineConfig{
			"shell": {Command: "/bin/cat"}, // intentionally zero timeout
		},
	}
	ext, err := BuildExtractorForEngine("shell", cfg, &bytes.Buffer{})
	require.NoError(t, err)
	require.NotNil(t, ext)
	// We can't easily observe the timeout from outside. This test
	// exists mainly to assert the constructor doesn't panic with a
	// zero timeout. The ShellExtractor's own unit tests cover the
	// 30s default semantics.
	_ = time.Second // suppress unused if time only used in default
}

func TestBuildExtractorForEngine_ShellUsesConfiguredCommand(t *testing.T) {
	cfg := config.PDFConfig{
		Engines: map[string]config.EngineConfig{
			"shell": {Command: "/bin/cat", Timeout: 5 * time.Second},
		},
	}
	ext, err := BuildExtractorForEngine("shell", cfg, &bytes.Buffer{})
	require.NoError(t, err)
	require.NotNil(t, ext)
	var _ port.PDFExtractor = ext
}
```

- [ ] **Step 2: Run test to verify it fails**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestBuild ./internal/app/ -v
```

Expected: FAIL with `BuildPDFExtractor undefined`.

- [ ] **Step 3: Implement the factory**

Create `internal/app/pdf.go`:

```go
package app

import (
	"fmt"
	"io"

	"uni-context/internal/adapter/pdf"
	"uni-context/internal/config"
	"uni-context/internal/port"
)

// BuildPDFExtractor returns the default extractor per cfg.Engine.
// Returns (nil, nil) when PDF is unconfigured — caller proceeds
// without PDF support, and IngestService.Create errors clearly if a
// PDF is passed (see service package).
func BuildPDFExtractor(cfg config.PDFConfig, log io.Writer) (port.PDFExtractor, error) {
	if cfg.Engine == "" {
		return nil, nil
	}
	return buildExtractor(cfg.Engine, cfg.Engines, log)
}

// BuildExtractorForEngine returns an extractor for an explicit engine
// name. Used by the CLI when --engine overrides the config default.
// Errors name the specific config key the user must set when the
// chosen engine lacks required config.
func BuildExtractorForEngine(name string, cfg config.PDFConfig, log io.Writer) (port.PDFExtractor, error) {
	return buildExtractor(name, cfg.Engines, log)
}

func buildExtractor(name string, engines map[string]config.EngineConfig, log io.Writer) (port.PDFExtractor, error) {
	switch name {
	case "gxpdf":
		return pdf.NewGxpdfExtractor(log), nil
	case "shell":
		ec, ok := engines["shell"]
		if !ok || ec.Command == "" {
			return nil, fmt.Errorf(
				"engine %q not configured (set pdf.engines.shell.command in config.yaml)", name)
		}
		return pdf.NewShellExtractor(ec.Command, ec.Timeout), nil
	case "http":
		ec, ok := engines["http"]
		if !ok || ec.URL == "" {
			return nil, fmt.Errorf(
				"engine %q not configured (set pdf.engines.http.url in config.yaml)", name)
		}
		return pdf.NewHttpExtractor(ec.URL, ec.Timeout, ec.AuthToken), nil
	default:
		return nil, fmt.Errorf(
			"unknown pdf engine %q (want gxpdf|shell|http)", name)
	}
}
```

- [ ] **Step 4: Run tests to verify pass**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/app/ -v
```

Expected: all factory tests PASS. (Existing app tests should also still pass.)

- [ ] **Step 5: Format and commit**

```
goimports -w internal/app/pdf.go internal/app/pdf_test.go
git add internal/app/pdf.go internal/app/pdf_test.go
git commit -m "feat(app): add PDF extractor factory

BuildPDFExtractor returns the configured default (nil when PDF
disabled). BuildExtractorForEngine builds an explicit engine by name
(used by --engine CLI override). Errors name the specific config key
to set when an engine is missing required config."
```

---

## Task 6: IngestService PDF branch + options + rollback extension

**The biggest task.** Adds the constructor option, the per-call option, the PDF branch with rollback + embed-skip, and 7 service tests.

**Files:**
- Modify: `internal/service/ingest.go` (add field, options, branch, rollback extension, embed-skip)
- Modify: `internal/service/ingest_test.go` (add 7 PDF tests)

**Interfaces:**
- Consumes: `port.PDFExtractor` (Task 2).
- Produces:
  - `service.IngestOption` type + `service.WithPDFExtractor(ext port.PDFExtractor) IngestOption`
  - `service.CreateOption` type + `service.WithExtractor(ext port.PDFExtractor) CreateOption`
  - `IngestService.Create` signature changes to accept `opts ...CreateOption`

- [ ] **Step 1: Read current ingest.go and existing tests**

```
Read internal/service/ingest.go (current state)
Read internal/service/ingest_test.go (existing test patterns + fakeRepo/fakeFS fixtures)
```

Confirm the test helpers you'll reuse (likely `newIngestFixture` or similar). The new tests must use the same patterns.

- [ ] **Step 2: Write the first failing test (ErrorsWithoutExtractor)**

Add to `internal/service/ingest_test.go`. The existing fixture `ingestFixture` (in `fixture_test.go`) exposes `repo *fakeRepo`, `fs port.FileStore`, `fsRoot string`, `svc *IngestService` — but no log writer. For tests that need to assert on log output, build a fresh `bytes.Buffer` and construct the service manually using `f.repo` + `f.fs`.

```go
func TestIngestService_Create_PDF_ErrorsWithoutExtractor(t *testing.T) {
	f := newIngestFixture(t)
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf) // no WithPDFExtractor

	_, err := svc.Create(context.Background(), Input{
		Scope:       domain.ScopeUser,
		Kind:        domain.KindNote,
		Source:      domain.SourceManual,
		OwnerUserID: "u1",
		Title:       "paper",
		Content:     "%PDF-1.4 fake",
		MIME:        "application/pdf",
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "pdf extraction not configured")
	assert.Contains(t, err.Error(), "pdf.engine")
}
```

- [ ] **Step 3: Run test to verify it fails**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestIngestService_Create_PDF_ErrorsWithoutExtractor ./internal/service/ -v
```

Expected: FAIL with `Create` does not accept options / `pdfExtractor` undefined.

- [ ] **Step 4: Add the option types and constructor variant**

Modify `internal/service/ingest.go`:

Add to struct:
```go
type IngestService struct {
	repo  port.ContextRepo
	fs    port.FileStore
	embed *EmbedService
	log   io.Writer
	pdfExtractor port.PDFExtractor // nil = PDF support disabled
}
```

Add option types after the struct:
```go
// IngestOption configures an IngestService at construction time.
type IngestOption func(*IngestService)

// WithPDFExtractor is the constructor-time option that enables PDF
// → text extraction. Without it, passing Input with MIME
// "application/pdf" returns a clear error. Distinct from the
// per-call WithExtractor (a CreateOption) — Go has no overloading.
func WithPDFExtractor(ext port.PDFExtractor) IngestOption {
	return func(s *IngestService) { s.pdfExtractor = ext }
}

// CreateOption configures a single Create call. Applied after the
// constructor options; overrides constructor defaults for this call.
type CreateOption func(*createConfig)

type createConfig struct {
	extractor port.PDFExtractor
}

// WithExtractor is the per-call override for the PDF extractor.
// Used when the CLI passes --engine to choose a different engine
// than the config default.
func WithExtractor(ext port.PDFExtractor) CreateOption {
	return func(c *createConfig) { c.extractor = ext }
}
```

Modify both constructors to accept variadic opts:
```go
func NewIngestService(repo port.ContextRepo, fs port.FileStore, log io.Writer, opts ...IngestOption) *IngestService {
	s := &IngestService{repo: repo, fs: fs, log: log}
	for _, opt := range opts { opt(s) }
	return s
}

func NewIngestServiceWithEmbedder(repo port.ContextRepo, fs port.FileStore, embed *EmbedService, log io.Writer, opts ...IngestOption) *IngestService {
	s := &IngestService{repo: repo, fs: fs, embed: embed, log: log}
	for _, opt := range opts { opt(s) }
	return s
}
```

Modify `Create` signature:
```go
func (s *IngestService) Create(ctx context.Context, in Input, opts ...CreateOption) (string, error) {
```

At the VERY TOP of `Create`, BEFORE the existing `item, err := domain.NewContextItem(...)` call (line 64), add the PDF branch. This ordering is critical because:

- `item.WordCount = countWords(in.Content)` (line 83) must count extracted text, not PDF bytes
- `item.Content = in.Content` (line 99, inline path) must hold extracted text
- The externalize step (line 90, `s.fs.Put([]byte(in.Content), mime)`) must store extracted text, not PDF bytes
- `NewContextItem` itself (line 64) may validate Input — modifying `in` after this is too late

```go
	// Apply per-call options over constructor defaults.
	cfg := createConfig{extractor: s.pdfExtractor}
	for _, opt := range opts { opt(&cfg) }

	// pdfURI is captured at function scope so the repo.Create rollback
	// below can clean up the PDF blob too. Empty when no PDF branch ran.
	var pdfURI string

	if in.MIME == "application/pdf" {
		if cfg.extractor == nil {
			return "", fmt.Errorf(
				"pdf extraction not configured: set pdf.engine in config or pass --engine")
		}
		// SourceMeta nil-guard: the PDF branch writes to in.SourceMeta
		// BEFORE the existing nil-check (line ~79-81) runs (that one
		// protects item.SourceMeta, built later). CLI always
		// initializes the map, but a future API caller could pass nil.
		if in.SourceMeta == nil {
			in.SourceMeta = map[string]any{}
		}
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

	// ↓↓↓ Existing code resumes here at the former line 64 ↓↓↓
	// item, err := domain.NewContextItem(in.Scope, in.Kind, ...)
```

Find the existing rollback block (lines ~105-115):
```go
	if err := s.repo.Create(ctx, item); err != nil {
		if item.ContentURI != "" {
			_ = s.fs.Delete(item.ContentURI)
		}
		return "", fmt.Errorf("persist item: %w", err)
	}
```

Extend to also delete pdfURI:
```go
	if err := s.repo.Create(ctx, item); err != nil {
		if item.ContentURI != "" {
			_ = s.fs.Delete(item.ContentURI)
		}
		if pdfURI != "" {
			_ = s.fs.Delete(pdfURI)
		}
		return "", fmt.Errorf("persist item: %w", err)
	}
```

Find the existing embed block (lines ~143-147):
```go
	if s.embed != nil {
		if err := s.embed.Embed(ctx, item.ID, item.Title, item.Content); err != nil {
			fmt.Fprintf(s.log, "warn: embed failed for %s: %v\n", item.ID, err)
		}
	}
```

Replace with scoped skip:
```go
	// Embed-skip is scoped to the PDF path via pdfURI != "" so we
	// don't change behavior for non-PDF empty-content items (existing
	// contract calls embed unconditionally).
	skipEmbed := pdfURI != "" && item.Content == "" && item.ContentURI == ""
	if s.embed != nil && !skipEmbed {
		if err := s.embed.Embed(ctx, item.ID, item.Title, item.Content); err != nil {
			fmt.Fprintf(s.log, "warn: embed failed for %s: %v\n", item.ID, err)
		}
	} else if s.embed != nil && skipEmbed {
		fmt.Fprintf(s.log, "warn: skipping embed for %s (empty extracted content)\n", item.ID)
	}
```

- [ ] **Step 5: Run first test to verify pass**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestIngestService_Create_PDF_ErrorsWithoutExtractor ./internal/service/ -v
```

Expected: PASS. If existing tests fail because they call `Create(ctx, in)` (no opts), that's fine — variadic makes the old call shape still valid.

- [ ] **Step 6: Write the remaining 6 tests**

Add to `internal/service/ingest_test.go`:

```go
// fakePDFExtractor is a port.PDFExtractor double for service tests.
// Records the bytes it was called with so tests can assert override
// behavior. Returns the configured text + err on each call.
type fakePDFExtractor struct {
	called   bool
	gotBytes []byte
	text     string
	err      error
}

func (f *fakePDFExtractor) Extract(_ context.Context, content []byte) (string, error) {
	f.called = true
	f.gotBytes = content
	return f.text, f.err
}

func TestIngestService_Create_PDF_ExtractsAndStoresBlob(t *testing.T) {
	f := newIngestFixture(t)
	ext := &fakePDFExtractor{text: "extracted body text"}
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf, WithPDFExtractor(ext))

	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Title: "paper",
		Content: "%PDF-1.4 fake", MIME: "application/pdf",
	})
	require.NoError(t, err)
	require.NotEmpty(t, id)
	require.True(t, ext.called, "extractor must be called")
	assert.Equal(t, "%PDF-1.4 fake", string(ext.gotBytes),
		"extractor receives the raw PDF bytes")

	item, err := f.repo.Get(context.Background(), id)
	require.NoError(t, err)
	assert.Equal(t, "extracted body text", item.Content,
		"Content is the extracted text")
	assert.Equal(t, "text/plain", item.ContentMIME,
		"MIME rewired to text/plain post-extraction")
	pdfURI, ok := item.SourceMeta["original_uri"].(string)
	require.True(t, ok, "SourceMeta.original_uri must be a string")
	assert.NotEmpty(t, pdfURI, "SourceMeta.original_uri must be set")
	assert.Equal(t, "application/pdf", item.SourceMeta["original_mime"])
}

// TestIngestService_Create_PDF_EmptyExtraction_StoresBlobEmptyContent locks
// in the image-only-PDF contract: extracted text is "" but the blob is still
// stored, AND the embed path is skipped (no title-only vectors).
//
// REQUIRES NewIngestServiceWithEmbedder — the "skipping embed" log only fires
// when s.embed != nil && skipEmbed. The embedder is wired using the SAME
// pattern as TestIngest_Create_TriggersEmbed_WhenConfigured (ingest_test.go).
func TestIngestService_Create_PDF_EmptyExtraction_StoresBlobEmptyContent(t *testing.T) {
	// Use SQLite-backed repo+vs so the embedder's vector write would
	// actually land if the skip logic didn't fire. This makes the test
	// positively assert "no vector written" rather than just "log line
	// present" — much stronger.
	vs, repo, db := newMemVectorStore(t)
	defer db.Close()
	emb := fake.New("fake-model", 8)
	embedSvc := NewEmbedService(emb, vs, repo, newMemFileStore(t), newMemEmbeddingRepo(t, db), io.Discard)

	// Capture log so we can assert on the skip warning.
	var logBuf bytes.Buffer
	ext := &fakePDFExtractor{text: ""} // image-only PDF
	ingestFS := newMemFileStore(t)
	svc := NewIngestServiceWithEmbedder(repo, ingestFS, embedSvc, &logBuf, WithPDFExtractor(ext))

	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Title: "image-only paper",
		Content: "%PDF-1.4 fake", MIME: "application/pdf",
	})
	require.NoError(t, err)
	require.NotEmpty(t, id)

	item, err := repo.Get(context.Background(), id)
	require.NoError(t, err)
	assert.Empty(t, item.Content, "Content is empty for image-only PDF")
	pdfURI, ok := item.SourceMeta["original_uri"].(string)
	require.True(t, ok)
	assert.NotEmpty(t, pdfURI, "PDF blob URI still captured")

	logStr := logBuf.String()
	assert.Contains(t, logStr, "pdf extraction yielded no text")
	assert.Contains(t, logStr, "skipping embed")

	// Stronger assertion: no vector was actually written.
	vecs, _ := emb.Embed(context.Background(), []string{"image-only paper\n\n"})
	hits, err := vs.Search(context.Background(), port.VectorQuery{
		Vector: vecs[0], Model: "fake-model", Limit: 10,
	})
	require.NoError(t, err)
	assert.Empty(t, hits, "no vector should be written for image-only PDF")
}

func TestIngestService_Create_PDF_PropagatesExtractorError(t *testing.T) {
	f := newIngestFixture(t)
	ext := &fakePDFExtractor{err: errors.New("encrypted pdf: password required")}
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf, WithPDFExtractor(ext))

	_, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Content: "fake", MIME: "application/pdf",
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "extract pdf")
	assert.Contains(t, err.Error(), "encrypted pdf")
}

func TestIngestService_Create_PDF_WithExtractorOverride(t *testing.T) {
	// Constructor default is nil (PDF not configured); per-call
	// override via WithExtractor supplies the extractor for this call.
	f := newIngestFixture(t)
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf) // no WithPDFExtractor

	ext := &fakePDFExtractor{text: "from override"}
	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Content: "fake", MIME: "application/pdf",
	}, WithExtractor(ext))
	require.NoError(t, err)
	require.NotEmpty(t, id)
	require.True(t, ext.called, "override extractor must be called")

	item, _ := f.repo.Get(context.Background(), id)
	assert.Equal(t, "from override", item.Content)
}

func TestIngestService_Create_PDF_LargeExtractedText_ExternalizesTextOnly(t *testing.T) {
	f := newIngestFixture(t)
	// Build extracted text > 4KB so existing externalization fires.
	big := strings.Repeat("a", 5000)
	ext := &fakePDFExtractor{text: big}
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf, WithPDFExtractor(ext))

	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Content: "%PDF fake", MIME: "application/pdf",
	})
	require.NoError(t, err)

	item, err := f.repo.Get(context.Background(), id)
	require.NoError(t, err)
	require.NotEmpty(t, item.ContentURI, "extracted text externalized → ContentURI set")
	assert.Empty(t, item.Content, "Content is empty when externalized")

	pdfURI, _ := item.SourceMeta["original_uri"].(string)
	require.NotEmpty(t, pdfURI, "PDF blob URI captured in SourceMeta")
	assert.NotEqual(t, item.ContentURI, pdfURI,
		"text URI and PDF URI must be distinct")
}

// TestIngestService_Create_PDF_RollsBackBothBlobsOnRepoFailure mirrors the
// existing TestIngest_Create_RollsBackFileStoreOnRepoFailure pattern: force
// repo.Create to fail, then walk fsRoot to confirm NO orphan files remain.
// In the PDF path, TWO fs.Put calls happen before repo.Create (PDF blob +
// externalized text), so rollback must fs.Delete BOTH. If only one is
// cleaned up, fsRoot will contain leftover files and the test fails.
func TestIngestService_Create_PDF_RollsBackBothBlobsOnRepoFailure(t *testing.T) {
	f := newIngestFixture(t)
	// Force repo.Create to fail. fakeRepo (fake_repo_test.go:15) exposes
	// the unexported createErr field directly; tests are in the same
	// package so we can set it.
	f.repo.createErr = errors.New("simulated DB outage")

	// Extracted text > 4KB so externalization fires BEFORE the failing
	// repo.Create. This means at the point repo.Create runs, fs has TWO
	// blobs: the PDF blob (Put in PDF branch) and the text blob (Put in
	// externalize step). Both must be rolled back.
	big := strings.Repeat("a", 5000)
	ext := &fakePDFExtractor{text: big}
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf, WithPDFExtractor(ext))

	_, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Content: "%PDF fake", MIME: "application/pdf",
	})
	require.Error(t, err)

	// fsRoot must be empty: both blobs deleted by the extended rollback.
	// Pattern lifted from TestIngest_Create_RollsBackFileStoreOnRepoFailure.
	var orphans []string
	walkErr := filepath.WalkDir(f.fsRoot, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		orphans = append(orphans, path)
		return nil
	})
	require.NoError(t, walkErr)
	assert.Empty(t, orphans,
		"both PDF blob and text blob must be rolled back; found orphaned files: %v", orphans)
}
```

**Notes on test fixtures:**
- `newIngestFixture(t)` returns `*ingestFixture{repo *fakeRepo, fs port.FileStore, fsRoot string, svc *IngestService}` — see `fixture_test.go`. No `log` field; tests that assert on log build their own `bytes.Buffer`.
- `fakeRepo.createErr` (unexported field on `fake_repo_test.go:15`) is the failure-injection point. Same-package tests set it directly. No `SetCreateHook` method exists.
- To detect rollback cleanup, walk `f.fsRoot` (matches the existing `TestIngest_Create_RollsBackFileStoreOnRepoFailure` pattern at `ingest_test.go:105`). No `DeletedURIs()` helper exists on the FS.
- For tests that exercise the embedder path, use the SQLite-backed `newMemVectorStore(t)` + `fake.New("fake-model", 8)` pattern (see `TestIngest_Create_TriggersEmbed_WhenConfigured` at `ingest_test.go:144`).

- [ ] **Step 7: Run all PDF tests to verify pass**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestIngestService_Create_PDF ./internal/service/ -v
```

Expected: all 7 PASS.

- [ ] **Step 8: Run full service test suite to verify no regression**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/service/ -v
```

Expected: all tests PASS (existing + new). If existing tests fail due to the `Create` signature change, fix them — but variadic opts should make the old `Create(ctx, in)` call shape still compile.

- [ ] **Step 9: Format and commit**

```
goimports -w internal/service/ingest.go internal/service/ingest_test.go
git add internal/service/ingest.go internal/service/ingest_test.go
git commit -m "feat(ingest): add PDF extraction branch with rollback + embed-skip

IngestService.Create gains an optional PDF branch:
- Constructs a port.PDFExtractor via constructor (WithPDFExtractor
  IngestOption) or per-call (WithExtractor CreateOption).
- Extracts text, stores raw PDF bytes in FileStore, sets
  SourceMeta[original_uri/mime].
- Rewires Input: Content=extracted text, MIME=text/plain, then
  flows through existing externalize/repo/FTS/embed path.

Rollback extended: on repo.Create failure, also fs.Delete(pdfURI).
Previously only item.ContentURI was cleaned up, orphaning the PDF
blob when the DB write failed.

Embed-skip scoped to PDF path (pdfURI != \"\" && empty Content):
avoid producing title-only vectors for image-only PDFs. Non-PDF
empty-content items keep their existing unconditional-embed behavior."
```

---

## Task 7: Wire PDF extractor in `app.Wire`

**Files:**
- Modify: `internal/app/app.go`

**Interfaces:**
- Consumes: `app.BuildPDFExtractor` (Task 5), `service.WithPDFExtractor` (Task 6).

- [ ] **Step 1: Write the failing test**

Add to `internal/app/app_test.go` (it already exists with `TestWire_EmbedderEnabled_ConstructsEmbeddingRepo` and `TestWire_EmbedderDisabled_LeavesEmbeddingFieldsNil` — follow the same shape):

```go
func TestWire_PDFEnabled_NoError(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		DataDir: dir,
		PDF:     config.PDFConfig{Engine: "gxpdf"},
	}
	a, err := Wire(cfg)
	require.NoError(t, err)
	t.Cleanup(func() { _ = a.Close() })
	// No public field exposes the extractor; the integration is verified
	// end-to-end by the CLI tests in Task 8. Here we just assert Wire
	// doesn't error when PDF is enabled with a valid engine.
}

func TestWire_PDFDisabled_NoError(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{DataDir: dir} // PDF zero-valued → Engine=""
	a, err := Wire(cfg)
	require.NoError(t, err)
	t.Cleanup(func() { _ = a.Close() })
}

func TestWire_PDFMisconfigured_Errors(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		DataDir: dir,
		PDF:     config.PDFConfig{Engine: "shell"}, // Engines map nil → no command
	}
	_, err := Wire(cfg)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "pdf")
}
```

- [ ] **Step 2: Run tests to verify which fail**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestWire_PDF ./internal/app/ -v
```

Expected: `TestWire_PDFEnabled_NoError` and `TestWire_PDFDisabled_NoError` PASS trivially (Wire currently ignores PDF entirely, so it succeeds regardless). `TestWire_PDFMisconfigured_Errors` FAILS — Wire doesn't yet validate PDF config, so it returns nil error and the `require.Error` assertion trips. That's the failing test that drives Step 3.

- [ ] **Step 3: Modify Wire to build and inject the extractor**

In `internal/app/app.go`, find the `ingest` construction block (around line 181-184):

```go
	ingest := service.NewIngestService(repo, fs, os.Stderr)
	if embedSvc != nil {
		ingest = service.NewIngestServiceWithEmbedder(repo, fs, embedSvc, os.Stderr)
	}
```

Replace with:

```go
	// PDF extractor (Task: pdf-attach). Built unconditionally so a
	// misconfigured engine (e.g. shell selected but no command) fails
	// Wire loudly. BuildPDFExtractor returns (nil, nil) when PDF is
	// unconfigured — the service then errors only if a PDF is actually
	// passed, which is the right UX for users who don't use PDFs.
	pdfExt, err := BuildPDFExtractor(cfg.PDF, os.Stderr)
	if err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("build pdf extractor: %w", err)
	}
	ingestOpts := []service.IngestOption{}
	if pdfExt != nil {
		ingestOpts = append(ingestOpts, service.WithPDFExtractor(pdfExt))
	}
	ingest := service.NewIngestService(repo, fs, os.Stderr, ingestOpts...)
	if embedSvc != nil {
		ingest = service.NewIngestServiceWithEmbedder(repo, fs, embedSvc, os.Stderr, ingestOpts...)
	}
```

- [ ] **Step 4: Run Wire tests to verify pass**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/app/ -v
```

Expected: all three new tests PASS.

- [ ] **Step 5: Run full test suite to verify no regression**

```
HTTPS_PROXY=socks5://127.0.0.1:7890 CGO_ENABLED=1 go test -tags 'sqlite_fts5' -count=1 ./...
```

Expected: PASS across all packages.

- [ ] **Step 6: Format and commit**

```
goimports -w internal/app/app.go internal/app/app_test.go
git add internal/app/app.go internal/app/app_test.go
git commit -m "feat(app): wire PDF extractor into IngestService

BuildPDFExtractor runs unconditionally so misconfigurations fail
loudly at Wire (e.g. shell engine without command). Returns nil
when PDF is unconfigured — service then errors only if a PDF is
actually passed. Both WithEmbedder and non-WithEmbedder branches
receive the same opts slice."
```

---

## Task 8: CLI surface — `mimeForFile` rename + `.pdf` accept + `--engine` flag + size cap bump

**Files:**
- Modify: `internal/cli/user_note.go`
- Modify: `internal/cli/user_note_test.go`
- Modify: `internal/cli/user_note_run_e_test.go`

**Interfaces:**
- Consumes: `app.BuildExtractorForEngine` (Task 5), `service.WithExtractor` (Task 6).

- [ ] **Step 1: Read current user_note.go and its tests**

```
Read internal/cli/user_note.go (full)
Read internal/cli/user_note_test.go (full)
Read internal/cli/user_note_run_e_test.go (full)
```

Identify:
- `mimeForTextFile` definition (line ~314)
- `maxFileBytes` constant (line ~308)
- `checkFileSize` tests (boundary values)
- `validateFileImport` and its tests
- `swapUserNoteLoadAppFn` and existing swap-pattern tests

- [ ] **Step 2: Update size cap boundary tests first (TDD: write failing test)**

Find existing `checkFileSize` boundary tests in `user_note_test.go`. They likely look like:

```go
func TestCheckFileSize_AtCap(t *testing.T) {
	assert.NoError(t, checkFileSize(10*1024*1024))
}
func TestCheckFileSize_OverCap(t *testing.T) {
	assert.Error(t, checkFileSize(10*1024*1024+1))
}
```

Update expected values to 50MB:

```go
func TestCheckFileSize_AtCap(t *testing.T) {
	assert.NoError(t, checkFileSize(50*1024*1024))
}
func TestCheckFileSize_OverCap(t *testing.T) {
	assert.Error(t, checkFileSize(50*1024*1024+1))
}
```

- [ ] **Step 3: Run tests to verify they fail**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestCheckFileSize ./internal/cli/ -v
```

Expected: FAIL (cap is still 10MB).

- [ ] **Step 4: Bump the cap**

In `internal/cli/user_note.go`, find `maxFileBytes` (line ~308) and change:

```go
const maxFileBytes int64 = 10 * 1024 * 1024
```

to:

```go
// maxFileBytes is the file import size cap. Bumped from 10MB → 50MB
// for PDF support (academic papers commonly 5-15MB, scanned textbooks
// 20-80MB). Text files rarely approach this; cap exists as a guardrail
// against accidentally loading huge files, not a security boundary.
const maxFileBytes int64 = 50 * 1024 * 1024
```

- [ ] **Step 5: Run tests to verify pass**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestCheckFileSize ./internal/cli/ -v
```

Expected: PASS.

- [ ] **Step 6: Update MIME detection test for the rename**

Find tests referencing `mimeForTextFile` and rename to `mimeForFile`. Add a PDF case:

```go
func TestMimeForFile(t *testing.T) {
	cases := []struct{ path, want string }{
		{"foo.md", "text/markdown"},
		{"foo.MARKDOWN", "text/markdown"}, // case-insensitive
		{"foo.txt", "text/plain"},
		{"foo.pdf", "application/pdf"},
		{"foo.PDF", "application/pdf"},
		{"foo.unknown", "text/plain"}, // backward compat fallback
		{"noext", "text/plain"},
	}
	for _, c := range cases {
		assert.Equal(t, c.want, mimeForFile(c.path), "path=%s", c.path)
	}
}
```

Run and verify it fails (`mimeForTextFile` is the actual name):

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestMimeForFile ./internal/cli/ -v
```

- [ ] **Step 7: Rename + extend MIME detection**

In `internal/cli/user_note.go`:

Find (around line 314):
```go
func mimeForTextFile(path string) string {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".md", ".markdown":
		return "text/markdown"
	default:
		return "text/plain"
	}
}
```

Replace with:
```go
// mimeForFile returns the MIME type for a path based on extension.
// Renamed from mimeForTextFile when PDF support was added (the old
// name lied once it returned application/pdf). Unknown extensions
// fall back to text/plain (backward compat for users who pass
// weirdly-named text files).
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

Find the call site (around line 81):
```go
mime = mimeForTextFile(noteFilePath)
```
Replace with:
```go
mime = mimeForFile(noteFilePath)
```

Update the `--file` flag's help text (around line 231):
```go
userNoteAddCmd.Flags().StringVar(&noteFilePath, "file", "", "import content from a file (text only)")
```
Replace with:
```go
userNoteAddCmd.Flags().StringVar(&noteFilePath, "file", "", "import content from a file (.txt, .md, .pdf)")
```

Update the long description (around line 50):
```go
  --file <path>     import from a .txt or .md file (max 10 MB)
```
Replace with:
```go
  --file <path>     import from a .txt, .md, or .pdf file (max 50 MB)
```

- [ ] **Step 8: Run MIME tests to verify pass**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestMimeForFile ./internal/cli/ -v
```

Expected: PASS.

- [ ] **Step 9: Add `--engine` flag**

Add a package-level var near the existing flag vars (line ~33):
```go
var pdfEngine string
```

Register the flag in `init()` (line ~228):
```go
userNoteAddCmd.Flags().StringVar(&pdfEngine, "engine", "",
	`pdf extractor override: "gxpdf", "shell", or "http". `+
		`Empty uses the config default (pdf.engine).`)
```

- [ ] **Step 9.5: Update `resetNoteFlags` test helper**

The existing `resetNoteFlags` in `internal/cli/user_note_run_e_test.go:66-78` resets package-level flag vars to prevent state leakage between tests. Without adding `pdfEngine = ""` to it, a test that sets `--engine bogus` will leak that value into the next test, causing flaky failures — especially in `TestUserNoteAdd_PDF_PassesExtractorOverride` if it runs after `TestUserNoteAdd_PDF_UnknownEngineValue_Errors`.

Modify `resetNoteFlags` (both the initial reset block AND the `t.Cleanup` block, since cobra flags persist after `Execute`):

```go
func resetNoteFlags(t *testing.T) {
	t.Helper()
	noteFilePath = ""
	noteTitle = ""
	noteTags = nil
	flagJSON = false
	pdfEngine = "" // NEW: reset between tests
	t.Cleanup(func() {
		noteFilePath = ""
		noteTitle = ""
		noteTags = nil
		flagJSON = false
		pdfEngine = "" // NEW: also reset on cleanup
	})
}
```

- [ ] **Step 10: Wire the runtime resolution into RunE**

In the `RunE` of `userNoteAddCmd`, near the top (before any IO — `--engine` validation must happen before file reads so users get the typo feedback instantly), add the validation:

```go
if pdfEngine != "" && pdfEngine != "gxpdf" && pdfEngine != "shell" && pdfEngine != "http" {
	return fmt.Errorf("unknown pdf engine %q (want gxpdf|shell|http)", pdfEngine)
}
```

Then, after building `service.Input` but before calling `a.Ingest.Create`, add the extractor resolution. The CLI does NOT use the constructor-configured default — it always builds an explicit extractor when the file is a PDF, so the per-call override carries cleanly. The constructor default is only the fallback for non-CLI callers (tests, future API):

```go
// Engine override: when --engine is set OR the file is a PDF, build
// an extractor explicitly. The constructor default from app.Wire only
// fires for non-PDF-aware callers — the CLI always takes this path
// for PDFs so the choice is per-invocation, not per-process.
var createOpts []service.CreateOption
if pdfEngine != "" || mime == "application/pdf" {
	engineName := pdfEngine
	if engineName == "" {
		engineName = cfg.PDF.Engine
	}
	if engineName == "" {
		return fmt.Errorf(
			"pdf extraction not configured: set pdf.engine in config or pass --engine")
	}
	ext, err := app.BuildExtractorForEngine(engineName, cfg.PDF, os.Stderr)
	if err != nil {
		return err
	}
	createOpts = append(createOpts, service.WithExtractor(ext))
}
```

Then change the existing `a.Ingest.Create` call to spread `createOpts`:

```go
id, err := a.Ingest.Create(cmd.Context(), service.Input{
	// ... existing fields unchanged ...
}, createOpts...)
```

(The variadic spread makes the no-PDF path a no-op — `createOpts` is nil-slice, which is a valid empty variadic.)

- [ ] **Step 11: Write CLI integration tests**

Add to `internal/cli/user_note_run_e_test.go`. Follow the existing swap-pattern tests (see `TestUserNoteAddCmd_RunEWithFileImport_PreservesFilenameAndMIME` at line 85 for the canonical structure).

The `swapUserNoteLoadAppFn(a)` helper at line 27 hardcodes `&config.Config{User: config.UserConfig{ID: "test-user"}}` — it ignores PDF config. For PDF tests that need a configured engine, swap the var directly so you control the Config:

```go
func TestUserNoteAdd_PDF_NoEngineNoConfig_Errors(t *testing.T) {
	repo := &capturingRepo{}
	a := newStubApp(t)
	a.Ingest = service.NewIngestService(repo, emptyFileStore{}, io.Discard)

	// Custom swap: PDF.Engine = "" (disabled)
	prev := userNoteLoadAppFn
	userNoteLoadAppFn = func() (*app.App, *config.Config, error) {
		return a, &config.Config{
			User: config.UserConfig{ID: "test-user"},
			PDF:  config.PDFConfig{Engine: ""}, // PDF not configured
		}, nil
	}
	t.Cleanup(func() { userNoteLoadAppFn = prev })
	resetNoteFlags(t)

	// Fake .pdf path — the file content doesn't matter because the
	// error fires before any extractor runs. Use a temp file so
	// validateFileImport's stat check passes.
	dir := t.TempDir()
	pdfPath := filepath.Join(dir, "paper.pdf")
	require.NoError(t, os.WriteFile(pdfPath, []byte("%PDF-1.4 fake"), 0o644))

	rootCmd.SetArgs([]string{"user", "note", "add", "--file", pdfPath})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	err := rootCmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "pdf extraction not configured")
	assert.Empty(t, repo.created, "no item should be created on validation failure")
}

func TestUserNoteAdd_PDF_UnknownEngineValue_Errors(t *testing.T) {
	repo := &capturingRepo{}
	a := newStubApp(t)
	a.Ingest = service.NewIngestService(repo, emptyFileStore{}, io.Discard)
	restore := swapUserNoteLoadAppFn(a)
	defer restore()
	resetNoteFlags(t)

	// --engine bogus fails validation BEFORE any IO. No file needed
	// on disk, but we still pass one so earlier checks (mutual
	// exclusion, stat) don't fire first.
	dir := t.TempDir()
	pdfPath := filepath.Join(dir, "paper.pdf")
	require.NoError(t, os.WriteFile(pdfPath, []byte("%PDF-1.4 fake"), 0o644))

	rootCmd.SetArgs([]string{"user", "note", "add", "--file", pdfPath, "--engine", "bogus"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	err := rootCmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "unknown pdf engine")
	assert.Contains(t, err.Error(), "bogus")
	assert.Empty(t, repo.created, "no item should be created on engine validation failure")
}

// TestUserNoteAdd_PDF_PassesExtractorOverride exercises the full path:
// --file paper.pdf + Config.PDF.Engine=shell with Engines[shell].Command
// pointing at a temp shell script that echoes canned text. Verifies the
// extractor runs end-to-end and the resulting item.Content matches.
func TestUserNoteAdd_PDF_PassesExtractorOverride(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("shell-engine test relies on chmod +x; skip on Windows")
	}
	// Stub script that prints canned extracted text to stdout, exits 0.
	stub, err := os.CreateTemp("", "pdf-stub-*")
	require.NoError(t, err)
	_, err = stub.WriteString("#!/bin/sh\necho 'shell extracted text'\n")
	require.NoError(t, err)
	require.NoError(t, stub.Close())
	require.NoError(t, os.Chmod(stub.Name(), 0o755))

	repo := &capturingRepo{}
	a := newStubApp(t)
	// No WithPDFExtractor here — the CLI passes the extractor per-call via
	// WithExtractor (BuildExtractorForEngine builds it from Config). The
	// constructor-level extractor from app.Wire is bypassed in this test
	// because we're constructing IngestService directly.
	a.Ingest = service.NewIngestService(repo, emptyFileStore{}, io.Discard)

	// Custom swap: PDF.Engine=shell, Engines.shell.Command=<stub>
	prev := userNoteLoadAppFn
	userNoteLoadAppFn = func() (*app.App, *config.Config, error) {
		return a, &config.Config{
			User: config.UserConfig{ID: "test-user"},
			PDF: config.PDFConfig{
				Engine: "shell",
				Engines: map[string]config.EngineConfig{
					"shell": {Command: stub.Name(), Timeout: 5 * time.Second},
				},
			},
		}, nil
	}
	t.Cleanup(func() { userNoteLoadAppFn = prev })
	resetNoteFlags(t)

	// Tiny .pdf fixture — content doesn't matter; the stub ignores stdin.
	dir := t.TempDir()
	pdfPath := filepath.Join(dir, "paper.pdf")
	require.NoError(t, os.WriteFile(pdfPath, []byte("%PDF-1.4 fake bytes"), 0o644))

	rootCmd.SetArgs([]string{"user", "note", "add", "--file", pdfPath, "--title", "paper"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	require.NoError(t, rootCmd.Execute())

	require.Len(t, repo.created, 1)
	item := repo.created[0]
	assert.Equal(t, "shell extracted text\n", item.Content,
		"Content must come from the shell extractor's stdout")
	assert.Equal(t, "text/plain", item.ContentMIME,
		"MIME rewired to text/plain post-extraction")
	// emptyFileStore.Put returns ("", "", nil), so pdfURI is "" here —
	// the *persistence* of the URI is covered by the service unit tests
	// (TestIngestService_Create_PDF_ExtractsAndStoresBlob asserts NotEmpty
	// against a real fsstore). This CLI test only asserts the key exists,
	// proving the PDF branch in Create ran and wrote to SourceMeta.
	_, ok := item.SourceMeta["original_uri"].(string)
	assert.True(t, ok, "SourceMeta.original_uri key must exist after PDF branch ran")
	assert.Equal(t, "application/pdf", item.SourceMeta["original_mime"])
}
```

Add imports to `user_note_run_e_test.go` as needed: `"os"`, `"path/filepath"`, `"runtime"`, `"time"`, `"uni-context/internal/config"`.

- [ ] **Step 12: Run CLI tests to verify pass**

```
CGO_ENABLED=1 go test -tags sqlite_fts5 -run TestUserNoteAdd_PDF ./internal/cli/ -v
```

Expected: all three new tests PASS.

- [ ] **Step 13: Run full test suite for regression check**

```
HTTPS_PROXY=socks5://127.0.0.1:7890 CGO_ENABLED=1 go test -tags 'sqlite_fts5' -count=1 ./...
```

Expected: PASS across all packages.

- [ ] **Step 14: Manual smoke test**

```
go build -tags sqlite_fts5 -o /tmp/unictx .
/tmp/unictx user note add --file docs/superpowers/specs/2026-06-26-pdf-attach-design.md  # text file still works
# Find or generate a small PDF and:
# /tmp/unictx user note add --file sample.pdf --engine gxpdf
# /tmp/unictx search "extracted"
# /tmp/unictx user note get <id>
```

Verify: the note is created, search hits the extracted text, get returns both Content and (via SourceMeta, if exposed) the original_uri.

- [ ] **Step 15: Format and commit**

```
goimports -w internal/cli/user_note.go internal/cli/user_note_test.go internal/cli/user_note_run_e_test.go
git add internal/cli/user_note.go internal/cli/user_note_test.go internal/cli/user_note_run_e_test.go
git commit -m "feat(cli): user note add --file paper.pdf

- Rename mimeForTextFile → mimeForFile; accept .pdf (returns
  application/pdf).
- Bump maxFileBytes 10MB → 50MB for realistic PDF sizes.
- Add --engine flag (gxpdf|shell|http); empty uses config default.
- Runtime resolution: build per-call extractor via
  app.BuildExtractorForEngine, pass via service.WithExtractor
  CreateOption. No-op when --file is a text file.

Friendly errors: unknown engine value, missing engine config, PDF
passed without any extractor configured."
```

---

## Verification (end-to-end)

After all 8 tasks land, run the full verification matrix:

```
HTTPS_PROXY=socks5://127.0.0.1:7890 CGO_ENABLED=1 go test -tags 'sqlite_fts5' -count=1 ./...
```

All packages must pass. Then manual smoke tests:

1. `unictx user note add --file sample.pdf` (no --engine) — uses config default.
2. `unictx user note add --file sample.pdf --engine shell` — uses shell stub via config.
3. `unictx user note add --file sample.pdf --engine bogus` — friendly error.
4. `unictx search <phrase>` — FTS hits the extracted body text.
5. `unictx user note get <id>` — Content is the extracted text.
6. `unictx user note add --file blank.pdf` — succeeds with empty Content + warning logged.
7. `unictx user note add --file huge.pdf` (>50MB) — size cap error.

### Final task: CHANGELOG entry

Append a section to `CHANGELOG.md` under a new release heading (or the in-progress "Known Limitations"/feature section if no release heading exists yet). Suggested wording — adapt to match the file's existing voice:

```markdown
## 2026-06-26 — PDF attach for `user note add`

`unictx user note add --file paper.pdf` now extracts text and stores both
the original PDF blob and the extracted text as a searchable, embeddable
context item.

- Default engine: `github.com/coregx/gxpdf` (pure Go, no external deps).
- Alternatives: `shell` (subprocess like `pdftotext - -`) and `http`
  (POST binary to a service).
- Per-call override via `--engine gxpdf|shell|http`.
- Image-only / scanned PDFs (no text layer) store the blob with empty
  Content + a warning; embed is skipped to avoid title-only vectors.
- Encrypted PDFs surface a clear "encrypted pdf: password required"
  error (no `--password` flag yet — see spec "Future work").
- File size cap bumped 10 MB → 50 MB for realistic PDF sizes.

Config schema:
  pdf:
    engine: gxpdf
    engines:
      shell: { command: 'pdftotext - -', timeout: 30s }
      http:  { url: 'http://localhost:8000/extract', timeout: 30s, auth_token: '...' }

Empty `pdf.engine` (the default) disables PDF support — `user note add
--file x.pdf` errors with a clear "pdf extraction not configured" until
the user opts in.
```

Commit the CHANGELOG update separately (or as part of Task 8's commit if you prefer atomic feature commits).

## Out of scope (deferred)

Per the spec:
- `--password` for encrypted PDFs
- `--pages 1-10` page-range selection
- Other binary formats (docx, html)
- `--no-size-limit` escape hatch

These are documented in the spec's "Future work" section and explicitly not implemented by this plan.
