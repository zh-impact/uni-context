package service

import (
	"context"
	"errors"
	"testing"

	"uni-context/internal/domain"
	"uni-context/internal/port"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// fakeListRepo is a minimal ContextRepo stub for ReembedService tests.
// Only List is exercised; other methods panic if called unexpectedly.
type fakeListRepo struct {
	items []domain.ContextItem
}

func (f *fakeListRepo) Create(ctx context.Context, item domain.ContextItem) error {
	panic("unexpected")
}
func (f *fakeListRepo) Update(ctx context.Context, item domain.ContextItem) (domain.ContextItem, error) {
	panic("unexpected")
}
func (f *fakeListRepo) Delete(ctx context.Context, id string) error {
	panic("unexpected")
}
func (f *fakeListRepo) Get(ctx context.Context, id string) (domain.ContextItem, error) {
	panic("unexpected")
}
func (f *fakeListRepo) List(ctx context.Context, f2 port.ItemFilter) ([]domain.ContextItem, string, error) {
	if f2.Limit > 0 && f2.Limit < len(f.items) {
		return f.items[:f2.Limit], "", nil
	}
	return f.items, "", nil
}
func (f *fakeListRepo) NextCursor(item domain.ContextItem) string { return "" }
func (f *fakeListRepo) ReindexFTS(_ context.Context, _, _, _, _ string) error {
	panic("unexpected")
}

// embedSpy captures Embed calls so tests can assert behavior without a
// real EmbedService. We can't substitute *EmbedService directly (concrete
// type), so tests construct a real EmbedService with a fake embedder and
// assert side effects via the embeddingRepo. For unit-test simplicity
// here, we instead inject the embed-call function via a thin wrapper.
//
// To keep the production ReembedService signature using *EmbedService,
// tests build a real EmbedService with port.Embedder = embedSpy.
type embedSpy struct {
	calls []string
	errOn map[string]error // itemID -> error to return
}

func (e *embedSpy) Model() port.ModelInfo {
	return port.ModelInfo{Slug: "active-model", Dimension: 8}
}
func (e *embedSpy) Embed(ctx context.Context, texts []string) ([][]float32, error) {
	e.calls = append(e.calls, texts[0])
	return [][]float32{make([]float32, 8)}, nil
}

// helper: build a real EmbedService whose embedder is the spy. The
// VectorStore and EmbeddingRepo are also fakes; EmbedService.Embed will
// exercise them, but for ReembedService tests we only care about call counts.
func newReembedServiceForTest(t *testing.T, items []domain.ContextItem, spy *embedSpy) (*ReembedService, *fakeEmbedRepo) {
	t.Helper()
	repo := &fakeListRepo{items: items}
	embRepo := &fakeEmbedRepo{statusByItem: map[string]port.EmbeddingStatus{}}
	// EmbedService deps: embedder, vs, repo, fs, embRepo.
	// For reembed tests we don't actually care about Put or hydration
	// correctness; we only count Embed calls via spy.calls. Use a fake
	// vs that always succeeds and a fake fs that returns empty content.
	embedSvc := NewEmbedService(spy, &noopVectorStore{}, &getItemRepo{items: items}, &emptyFileStore{}, embRepo)
	return NewReembedService(repo, embedSvc, port.ModelInfo{Slug: "active-model", Dimension: 8}), embRepo
}

func TestReembedService_DryRunDoesNotEmbed(t *testing.T) {
	items := []domain.ContextItem{
		{ID: "i1", Title: "t1", Content: "c1"},
		{ID: "i2", Title: "t2", Content: "c2"},
	}
	svc, _ := newReembedServiceForTest(t, items, &embedSpy{errOn: nil})

	report, err := svc.Run(context.Background(), 0, true)
	require.NoError(t, err)
	assert.Equal(t, 2, report.Scanned)
	assert.Equal(t, 0, report.Embedded)
	assert.Equal(t, 0, report.Failed)
}

func TestReembedService_EmbedsAllItemsWhenNoneDone(t *testing.T) {
	items := []domain.ContextItem{
		{ID: "i1", Title: "t1", Content: "c1"},
		{ID: "i2", Title: "t2", Content: "c2"},
	}
	spy := &embedSpy{}
	svc, _ := newReembedServiceForTest(t, items, spy)

	report, err := svc.Run(context.Background(), 0, false)
	require.NoError(t, err)
	assert.Equal(t, 2, report.Scanned)
	assert.Equal(t, 2, report.Embedded)
	assert.Equal(t, 0, report.Failed)
}

func TestReembedService_ProcessesItemsDoneForOtherModelsOnly(t *testing.T) {
	// An item done under 'bge-m3' but not under active 'active-model'
	// must be re-embedded.
	items := []domain.ContextItem{{ID: "i1", Title: "t1", Content: "c1"}}
	spy := &embedSpy{}
	svc, embRepo := newReembedServiceForTest(t, items, spy)
	embRepo.statusByItem["i1"] = port.EmbeddingStatus{
		ItemID: "i1", ModelSlug: "bge-m3", Status: "done",
	}

	report, err := svc.Run(context.Background(), 0, false)
	require.NoError(t, err)
	assert.Equal(t, 1, report.Scanned)
	assert.Equal(t, 1, report.Embedded, "other-model done row does not exclude")
}

func TestReembedService_LimitHonored(t *testing.T) {
	items := []domain.ContextItem{
		{ID: "i1"}, {ID: "i2"}, {ID: "i3"},
	}
	spy := &embedSpy{}
	svc, _ := newReembedServiceForTest(t, items, spy)

	report, err := svc.Run(context.Background(), 2, false)
	require.NoError(t, err)
	assert.Equal(t, 2, report.Scanned)
}

func TestReembedService_FailureContinuesAndRecords(t *testing.T) {
	items := []domain.ContextItem{
		{ID: "ok", Title: "t-ok", Content: "c"},
		{ID: "boom", Title: "t-boom", Content: "c"},
		{ID: "ok2", Title: "t-ok2", Content: "c"},
	}
	// Custom spy that errors on the "boom" item.
	spy := &failingEmbedSpy{failOn: "t-boom\n\nc"}
	embRepo := &fakeEmbedRepo{statusByItem: map[string]port.EmbeddingStatus{}}
	repo := &fakeListRepo{items: items}
	embedSvc := NewEmbedService(spy, &noopVectorStore{}, &getItemRepo{items: items}, &emptyFileStore{}, embRepo)
	svc := NewReembedService(repo, embedSvc, port.ModelInfo{Slug: "active-model", Dimension: 8})

	report, err := svc.Run(context.Background(), 0, false)
	require.NoError(t, err)
	assert.Equal(t, 3, report.Scanned)
	assert.Equal(t, 2, report.Embedded)
	assert.Equal(t, 1, report.Failed)
	require.Len(t, report.Failures, 1)
	assert.Contains(t, report.Failures[0].Error, "boom")
}

// failingEmbedSpy returns an error for any input containing failOn.
type failingEmbedSpy struct{ failOn string }

func (e *failingEmbedSpy) Model() port.ModelInfo {
	return port.ModelInfo{Slug: "active-model", Dimension: 8}
}
func (e *failingEmbedSpy) Embed(ctx context.Context, texts []string) ([][]float32, error) {
	for _, t := range texts {
		if t == e.failOn {
			return nil, errors.New("boom: synthetic embed failure")
		}
	}
	return [][]float32{make([]float32, 8)}, nil
}

// noopVectorStore accepts all puts; never returns hits on search.
type noopVectorStore struct{}

func (noopVectorStore) Put(ctx context.Context, model, itemID string, v []float32) error {
	return nil
}
func (noopVectorStore) Search(ctx context.Context, q port.VectorQuery) ([]port.VectorHit, error) {
	return nil, nil
}
func (noopVectorStore) Delete(ctx context.Context, model, itemID string) error { return nil }

// emptyFileStore returns empty bytes for any URI.
// NOTE: signature matches port.FileStore, which differs from the brief's
// draft (Put(name, data)). Adapted to the real interface.
type emptyFileStore struct{}

func (emptyFileStore) Put(content []byte, mime string) (uri string, hash string, err error) {
	return "", "", nil
}
func (emptyFileStore) Get(uri string) ([]byte, error) { return nil, nil }
func (emptyFileStore) Delete(uri string) error        { return nil }

// getItemRepo returns canned items by ID; used so EmbedService.repo.Get
// works in tests.
type getItemRepo struct{ items []domain.ContextItem }

func (r *getItemRepo) Create(ctx context.Context, item domain.ContextItem) error { return nil }
func (r *getItemRepo) Update(ctx context.Context, item domain.ContextItem) (domain.ContextItem, error) {
	return item, nil
}
func (r *getItemRepo) Delete(ctx context.Context, id string) error { return nil }
func (r *getItemRepo) Get(ctx context.Context, id string) (domain.ContextItem, error) {
	for _, it := range r.items {
		if it.ID == id {
			return it, nil
		}
	}
	return domain.ContextItem{}, errors.New("not found")
}
func (r *getItemRepo) List(ctx context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
	return r.items, "", nil
}
func (r *getItemRepo) NextCursor(item domain.ContextItem) string { return "" }
func (r *getItemRepo) ReindexFTS(_ context.Context, _, _, _, _ string) error {
	panic("unexpected")
}

// fakeEmbedRepo mirrors the one in service/embed_test.go. If that file
// already declares a conflicting name, rename this one.
type fakeEmbedRepo struct {
	statusByItem map[string]port.EmbeddingStatus
	putCalls     int
}

func (f *fakeEmbedRepo) UpsertStatus(ctx context.Context, itemID, modelSlug, status, errStr string) error {
	f.putCalls++
	existing := f.statusByItem[itemID]
	existing.ItemID = itemID
	existing.ModelSlug = modelSlug
	existing.Status = status
	existing.Attempts++
	if status == "failed" {
		existing.LastError = errStr
	}
	f.statusByItem[itemID] = existing
	return nil
}
func (f *fakeEmbedRepo) GetStatus(ctx context.Context, itemID, modelSlug string) (port.EmbeddingStatus, error) {
	s, ok := f.statusByItem[itemID]
	if !ok {
		return port.EmbeddingStatus{}, errors.New("not found")
	}
	return s, nil
}
func (f *fakeEmbedRepo) ListFailed(ctx context.Context, limit int) ([]port.EmbeddingStatus, error) {
	return nil, nil
}

// ListForItem satisfies the Plan 2c follow-up port.EmbeddingRepo addition.
// Not exercised by reembed tests; returns empty slice (not nil) to match
// the production contract.
func (f *fakeEmbedRepo) ListForItem(ctx context.Context, itemID string) ([]port.EmbeddingStatus, error) {
	return []port.EmbeddingStatus{}, nil
}
