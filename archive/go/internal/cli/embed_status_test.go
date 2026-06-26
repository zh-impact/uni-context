package cli

import (
	"bytes"
	"context"
	"errors"
	"os"
	"strings"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/app"
	"uni-context/internal/config"
	"uni-context/internal/port"
	"uni-context/internal/service"
)

// stubRegistry captures Register/List/Remove/SetDefault calls for the
// RunE tests. Methods not exercised by a given test panic so accidental
// use surfaces loudly.
type stubRegistry struct {
	registered []port.ModelSpec
	listCalled bool
	removed    []string
	setDefault []string
	errOn      map[string]error // method name -> error to return
}

func (s *stubRegistry) List(ctx context.Context) ([]port.ModelDescriptor, error) {
	s.listCalled = true
	if s.errOn != nil {
		if err, ok := s.errOn["List"]; ok {
			return nil, err
		}
	}
	return []port.ModelDescriptor{
		{Slug: "bge-m3", Provider: "ollama", Dimension: 1024,
			VecTable: "vec_bge_m3_1024", IsDefault: true, Status: "active"},
	}, nil
}
func (s *stubRegistry) GetActive(ctx context.Context) (port.ModelDescriptor, error) {
	return port.ModelDescriptor{Slug: "bge-m3", Dimension: 1024, IsDefault: true}, nil
}
func (s *stubRegistry) Get(ctx context.Context, slug string) (port.ModelDescriptor, error) {
	return port.ModelDescriptor{}, errors.New("not found")
}
func (s *stubRegistry) Register(ctx context.Context, spec port.ModelSpec) error {
	s.registered = append(s.registered, spec)
	return nil
}
func (s *stubRegistry) UpdateConfig(ctx context.Context, slug, baseURL, apiKey, provider string) error {
	return nil
}
func (s *stubRegistry) SetDefault(ctx context.Context, slug string) error {
	s.setDefault = append(s.setDefault, slug)
	return nil
}
func (s *stubRegistry) Remove(ctx context.Context, slug string) error {
	s.removed = append(s.removed, slug)
	return nil
}

// stubEmbeddingRepo captures ListForItem calls and returns a canned slice.
type stubEmbeddingRepo struct {
	rows   []port.EmbeddingStatus
	err    error
	called bool
}

func (s *stubEmbeddingRepo) UpsertStatus(ctx context.Context, itemID, modelSlug, status, errStr string) error {
	return nil
}
func (s *stubEmbeddingRepo) GetStatus(ctx context.Context, itemID, modelSlug string) (port.EmbeddingStatus, error) {
	return port.EmbeddingStatus{}, errors.New("not found")
}
func (s *stubEmbeddingRepo) ListFailed(ctx context.Context, limit int) ([]port.EmbeddingStatus, error) {
	return nil, nil
}
func (s *stubEmbeddingRepo) ListForItem(ctx context.Context, itemID string) ([]port.EmbeddingStatus, error) {
	s.called = true
	return s.rows, s.err
}

// swapLoadAppFn swaps the package-level loadAppFn to return a stubbed
// *App. Returns a restore func — tests MUST defer it. Not safe for
// parallel tests in the same package (loadAppFn is package-level).
func swapLoadAppFn(a *app.App) func() {
	prev := loadAppFn
	loadAppFn = func() (*app.App, *config.Config, error) {
		return a, &config.Config{}, nil
	}
	return func() { loadAppFn = prev }
}

// newStubApp returns a minimal *app.App suitable for RunE tests. The
// infra fields (db, repo, searcher, ...) are unexported, so the cli
// package cannot set them — and does not need to: every RunE handler
// reads only the exported service fields (Models, Backfill, Worker,
// Reembed, Items, Diagnostics, ...). App.Close handles a nil db, so the
// `defer a.Close()` registered by every RunE handler is a no-op on the
// early-exit branches.
func newStubApp(t *testing.T) *app.App {
	t.Helper()
	return &app.App{}
}

// captureStdout swaps os.Stdout for a pipe, runs fn, then restores
// os.Stdout and returns whatever was written. The embed RunE handlers
// write via `fmt.Printf` / `tabwriter.NewWriter(os.Stdout, ...)` (the
// codebase convention — see embedModelListCmd), so cobra's
// `cmd.SetOut(buf)` does NOT capture their output. Only an os.Stdout
// redirect works. Not safe for parallel tests in the same package.
func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	old := os.Stdout
	r, w, err := os.Pipe()
	require.NoError(t, err)
	os.Stdout = w
	defer func() { os.Stdout = old }()

	fn()
	require.NoError(t, w.Close())
	var buf bytes.Buffer
	_, err = buf.ReadFrom(r)
	require.NoError(t, err)
	return buf.String()
}

// TestEmbedStatusCmd_DisabledEmbedderErrorsCleanly: when App.EmbeddingRepo
// is nil (embedder.enabled=false), the command must return a clear error
// rather than nil-pointer-panic on ListForItem.
func TestEmbedStatusCmd_DisabledEmbedderErrorsCleanly(t *testing.T) {
	a := newStubApp(t) // EmbeddingRepo is nil
	restore := swapLoadAppFn(a)
	defer restore()

	rootCmd.SetArgs([]string{"embed", "status", "some-id"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	err := rootCmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "embedder not enabled")
}

// TestEmbedStatusCmd_NoRowsPrintsMessage: empty slice (not nil) from
// ListForItem must produce a friendly "no rows" line on stdout, not an
// empty table.
func TestEmbedStatusCmd_NoRowsPrintsMessage(t *testing.T) {
	repo := &stubEmbeddingRepo{rows: []port.EmbeddingStatus{}}
	a := newStubApp(t)
	a.Models = service.NewModelService(nil, repo)
	restore := swapLoadAppFn(a)
	defer restore()

	rootCmd.SetArgs([]string{"embed", "status", "absent-id"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	out := captureStdout(t, func() {
		require.NoError(t, rootCmd.Execute())
	})
	assert.Contains(t, out, "no embedding status rows for item absent-id")
}

// TestEmbedStatusCmd_PrintsTabularOutput: with 2 rows, the table header
// and both rows must render. LastError column truncates at 40 chars.
func TestEmbedStatusCmd_PrintsTabularOutput(t *testing.T) {
	longErr := strings.Repeat("e", 50)
	repo := &stubEmbeddingRepo{rows: []port.EmbeddingStatus{
		{ItemID: "i1", ModelSlug: "aaa-model", Status: "done", Attempts: 1, LastError: ""},
		{ItemID: "i1", ModelSlug: "zzz-model", Status: "failed", Attempts: 3, LastError: longErr},
	}}
	a := newStubApp(t)
	a.Models = service.NewModelService(nil, repo)
	restore := swapLoadAppFn(a)
	defer restore()

	rootCmd.SetArgs([]string{"embed", "status", "i1"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	out := captureStdout(t, func() {
		require.NoError(t, rootCmd.Execute())
	})

	assert.Contains(t, out, "MODEL_SLUG")
	assert.Contains(t, out, "aaa-model")
	assert.Contains(t, out, "zzz-model")
	assert.Contains(t, out, strings.Repeat("e", 37)+"...",
		"last_error column truncates to 37 chars + '...'")
}

// TestEmbedStatusCmd_ArgCountRejected: cobra's ExactArgs(1) must reject
// 2-arg invocation. (RunE never runs, so DB-Close defer never registers;
// the newStubApp call is for shape consistency only.)
func TestEmbedStatusCmd_ArgCountRejected(t *testing.T) {
	a := newStubApp(t)
	restore := swapLoadAppFn(a)
	defer restore()

	rootCmd.SetArgs([]string{"embed", "status", "a", "b"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	err := rootCmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "accepts 1 arg(s)")
}
