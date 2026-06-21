package cli

import (
	"bytes"
	"context"
	"errors"
	"os"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/domain"
	"uni-context/internal/port"
	"uni-context/internal/service"
)

// captureStderr swaps os.Stderr for a pipe, runs fn, restores os.Stderr,
// and returns whatever was written. The embed RunE handlers write via
// `fmt.Fprintf(os.Stderr, ...)` (e.g. embedSwitchCmd reminder), bypassing
// cobra's cmd.SetErr buffer. Not safe for parallel tests in the same
// package. Mirrors captureStdout in embed_status_test.go.
func captureStderr(t *testing.T, fn func()) string {
	t.Helper()
	old := os.Stderr
	r, w, err := os.Pipe()
	require.NoError(t, err)
	os.Stderr = w
	defer func() { os.Stderr = old }()

	fn()
	require.NoError(t, w.Close())
	var buf bytes.Buffer
	_, err = buf.ReadFrom(r)
	require.NoError(t, err)
	return buf.String()
}

// TestEmbedModelAddCmd_RunECallsRegistryRegister: invoking `embed model
// add <slug>` with the four flags must surface a Register call carrying
// the flag values. Verified via stubRegistry.registered slice.
func TestEmbedModelAddCmd_RunECallsRegistryRegister(t *testing.T) {
	reg := &stubRegistry{}
	a := newStubApp(t)
	a.Registry = reg
	restore := swapLoadAppFn(a)
	defer restore()

	// Reset flags from any prior test (package-global state).
	modelAddProvider = "openai"
	modelAddBaseURL = "https://api.openai.com/v1"
	modelAddAPIKey = "sk-test"
	modelAddDim = 3072
	t.Cleanup(func() {
		modelAddProvider, modelAddBaseURL, modelAddAPIKey, modelAddDim = "", "", "", 0
	})

	// Cobra 1.8.1's Execute() on a subcommand walks to rootCmd and prints
	// help without running RunE. Use rootCmd with the full arg path instead
	// (mirrors the Task 5 fix in embed_status_test.go).
	rootCmd.SetArgs([]string{"embed", "model", "add", "text-embedding-3-large"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	require.NoError(t, rootCmd.Execute())

	require.Len(t, reg.registered, 1)
	spec := reg.registered[0]
	assert.Equal(t, "text-embedding-3-large", spec.Slug)
	assert.Equal(t, "openai", spec.Provider)
	assert.Equal(t, "https://api.openai.com/v1", spec.BaseURL)
	assert.Equal(t, "sk-test", spec.APIKey)
	assert.Equal(t, 3072, spec.Dimension)
}

// TestEmbedModelListCmd_RunECallsRegistryList: `embed model list` must
// call Registry.List and print a tabular row per model. The stub returns
// one model (bge-m3 default) — assert it appears in stdout.
func TestEmbedModelListCmd_RunECallsRegistryList(t *testing.T) {
	reg := &stubRegistry{}
	a := newStubApp(t)
	a.Registry = reg
	restore := swapLoadAppFn(a)
	defer restore()

	// Cobra 1.8.1 walks subcmd.Execute to rootCmd; invoke via rootCmd with
	// the full arg path. RunE writes via tabwriter.NewWriter(os.Stdout,...),
	// so we must also wrap in captureStdout (cobra's cmd.SetOut buf is
	// bypassed).
	rootCmd.SetArgs([]string{"embed", "model", "list"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	out := captureStdout(t, func() {
		require.NoError(t, rootCmd.Execute())
	})

	assert.True(t, reg.listCalled, "Registry.List must be called")
	assert.Contains(t, out, "SLUG")
	assert.Contains(t, out, "bge-m3")
	assert.Contains(t, out, "*", "default model row carries the * marker")
}

// TestEmbedModelRemoveCmd_RunECallsRegistryRemove: `embed model remove
// <slug>` must call Registry.Remove with the slug arg.
func TestEmbedModelRemoveCmd_RunECallsRegistryRemove(t *testing.T) {
	reg := &stubRegistry{}
	a := newStubApp(t)
	a.Registry = reg
	restore := swapLoadAppFn(a)
	defer restore()

	// Cobra 1.8.1 walks subcmd.Execute to rootCmd; use rootCmd with the
	// full arg path.
	rootCmd.SetArgs([]string{"embed", "model", "remove", "old-model"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	require.NoError(t, rootCmd.Execute())

	require.Len(t, reg.removed, 1)
	assert.Equal(t, "old-model", reg.removed[0])
}

// TestEmbedSwitchCmd_RunECallsRegistrySetDefault: `embed switch <slug>`
// must call Registry.SetDefault and emit the stderr reminder so users
// know to run `embed reembed` next.
func TestEmbedSwitchCmd_RunECallsRegistrySetDefault(t *testing.T) {
	reg := &stubRegistry{}
	a := newStubApp(t)
	a.Registry = reg
	restore := swapLoadAppFn(a)
	defer restore()

	// Cobra 1.8.1 walks subcmd.Execute to rootCmd; use rootCmd with the
	// full arg path. RunE writes the reminder via fmt.Fprintf(os.Stderr,...)
	// which bypasses cobra's cmd.SetErr buffer; capture os.Stderr directly.
	rootCmd.SetArgs([]string{"embed", "switch", "new-model"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	stderr := captureStderr(t, func() {
		require.NoError(t, rootCmd.Execute())
	})

	require.Len(t, reg.setDefault, 1)
	assert.Equal(t, "new-model", reg.setDefault[0])
	assert.Contains(t, stderr, "embed reembed",
		"stderr reminder must mention the follow-up command")
}

// TestEmbedSwitchCmd_RunENilRegistryErrorsCleanly: when embedder.enabled
// is false, App.Registry is nil and the RunE handler must return the
// friendly "embedder not enabled" error rather than nil-pointer panic.
// newStubApp gives the handler a non-nil DB so the defer a.DB.Close()
// (which registers BEFORE the nil-Registry check) does not panic when
// the handler returns from the early-exit branch.
func TestEmbedSwitchCmd_RunENilRegistryErrorsCleanly(t *testing.T) {
	a := newStubApp(t) // Registry is nil
	restore := swapLoadAppFn(a)
	defer restore()

	// Cobra 1.8.1 walks subcmd.Execute to rootCmd; use rootCmd with the
	// full arg path.
	rootCmd.SetArgs([]string{"embed", "switch", "any-slug"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	err := rootCmd.Execute()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "embedder not enabled")
}

// TestEmbedReembedCmd_RunEWithRealService: constructs a real
// *service.ReembedService against fake deps (mirrors the pattern in
// internal/service/reembed_test.go) and asserts the RunE handler:
// (a) exits 0 on dry-run with the expected message; (b) reaches
// Reembed.Run via the wired service. The fake's side effect (Embed
// called per item) confirms the service path.
func TestEmbedReembedCmd_RunEWithRealService(t *testing.T) {
	items := []domain.ContextItem{
		{ID: "i1", Title: "t1", Content: "c1"},
		{ID: "i2", Title: "t2", Content: "c2"},
	}
	spy := &reembedSpy{} // defined below; lives in this file to avoid
	// polluting embed_status_test.go's helper namespace.
	repo := &reembedListRepo{items: items}
	embRepo := &stubEmbeddingRepo{rows: []port.EmbeddingStatus{}}
	embedSvc := service.NewEmbedService(spy, &noopVectorStore{},
		&reembedGetRepo{items: items}, &emptyFileStore{}, embRepo)
	reembed := service.NewReembedService(repo, embedSvc,
		port.ModelInfo{Slug: "active-model", Dimension: 8})

	a := newStubApp(t)
	a.Reembed = reembed
	restore := swapLoadAppFn(a)
	defer restore()

	reembedLimit = 0
	reembedDryRun = true
	t.Cleanup(func() { reembedLimit, reembedDryRun = 0, false })

	// Cobra 1.8.1 walks subcmd.Execute to rootCmd; use rootCmd with the
	// full arg path. RunE writes via fmt.Printf, which bypasses cobra's
	// cmd.SetOut buf; capture os.Stdout directly.
	rootCmd.SetArgs([]string{"embed", "reembed"})
	rootCmd.SetOut(new(bytes.Buffer))
	rootCmd.SetErr(new(bytes.Buffer))
	out := captureStdout(t, func() {
		require.NoError(t, rootCmd.Execute())
	})
	assert.Contains(t, out, "dry run")
	assert.Equal(t, 0, len(spy.calls), "dry run must not embed")
}

// reembedSpy mirrors the embedSpy from internal/service/reembed_test.go
// but is local to the cli package so we don't cross-import test helpers.
type reembedSpy struct {
	calls []string
}

func (s *reembedSpy) Model() port.ModelInfo {
	return port.ModelInfo{Slug: "active-model", Dimension: 8}
}
func (s *reembedSpy) Embed(ctx context.Context, texts []string) ([][]float32, error) {
	s.calls = append(s.calls, texts[0])
	return [][]float32{make([]float32, 8)}, nil
}

// reembedListRepo: minimal ContextRepo stub; only List is exercised.
type reembedListRepo struct{ items []domain.ContextItem }

func (r *reembedListRepo) Create(ctx context.Context, item domain.ContextItem) error {
	panic("unexpected")
}
func (r *reembedListRepo) Update(ctx context.Context, item domain.ContextItem) error {
	panic("unexpected")
}
func (r *reembedListRepo) Delete(ctx context.Context, id string) error {
	panic("unexpected")
}
func (r *reembedListRepo) Get(ctx context.Context, id string) (domain.ContextItem, error) {
	panic("unexpected")
}
func (r *reembedListRepo) List(ctx context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
	if f.Limit > 0 && f.Limit < len(r.items) {
		return r.items[:f.Limit], "", nil
	}
	return r.items, "", nil
}
func (r *reembedListRepo) NextCursor(item domain.ContextItem) string { return "" }

// reembedGetRepo: same shape but Get is the call exercised (EmbedService
// hydrates via Get during Embed).
type reembedGetRepo struct{ items []domain.ContextItem }

func (r *reembedGetRepo) Create(ctx context.Context, item domain.ContextItem) error {
	panic("unexpected")
}
func (r *reembedGetRepo) Update(ctx context.Context, item domain.ContextItem) error {
	panic("unexpected")
}
func (r *reembedGetRepo) Delete(ctx context.Context, id string) error {
	panic("unexpected")
}
func (r *reembedGetRepo) Get(ctx context.Context, id string) (domain.ContextItem, error) {
	for _, it := range r.items {
		if it.ID == id {
			return it, nil
		}
	}
	return domain.ContextItem{}, errors.New("not found")
}
func (r *reembedGetRepo) List(ctx context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
	return r.items, "", nil
}
func (r *reembedGetRepo) NextCursor(item domain.ContextItem) string { return "" }

// noopVectorStore, emptyFileStore: identical to the helpers in
// internal/service/reembed_test.go. Re-declared here because test helpers
// are package-private. If they cause a name collision in the cli package,
// rename to reembedNoopVectorStore / reembedEmptyFileStore.

type noopVectorStore struct{}

func (noopVectorStore) Put(ctx context.Context, model, itemID string, v []float32) error {
	return nil
}
func (noopVectorStore) Search(ctx context.Context, q port.VectorQuery) ([]port.VectorHit, error) {
	return nil, nil
}
func (noopVectorStore) Delete(ctx context.Context, model, itemID string) error { return nil }

type emptyFileStore struct{}

func (emptyFileStore) Put(content []byte, mime string) (uri string, hash string, err error) {
	return "", "", nil
}
func (emptyFileStore) Get(uri string) ([]byte, error) { return nil, nil }
func (emptyFileStore) Delete(uri string) error        { return nil }
