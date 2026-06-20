package service

import (
	"context"
	"errors"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/adapter/embedder/fake"
	"uni-context/internal/domain"
	"uni-context/internal/port"
)

func newEmbedFixture(t *testing.T) (*embedFixture, func()) {
	t.Helper()
	vs, repo, db := newMemVectorStore(t)
	emb := fake.New("fake-model", 8)
	fs := newMemFileStore(t)              // Plan 2b: hydration target
	embRepo := newMemEmbeddingRepo(t, db) // Plan 2b: shared DB with repo
	svc := NewEmbedService(emb, vs, repo, fs, embRepo)
	cleanup := func() { _ = db.Close() }
	return &embedFixture{
		repo: repo, vs: vs, emb: emb, fs: fs, embRepo: embRepo, svc: svc,
	}, cleanup
}

type embedFixture struct {
	repo    port.ContextRepo
	vs      port.VectorStore
	emb     *fake.Embedder
	fs      port.FileStore
	embRepo port.EmbeddingRepo
	svc     *EmbedService
}

func TestEmbedService_EmbedWritesVectorAndFlipsFlag(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	ctx := context.Background()
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "deploy guide"
	item.Content = "how to deploy go to k8s"
	require.NoError(t, f.repo.Create(ctx, item))

	require.NoError(t, f.svc.Embed(ctx, item.ID, item.Title, item.Content))

	// Vector is searchable: query with the fake's embedding of the same
	// composed text.
	vecs, err := f.emb.Embed(ctx, []string{item.Title + "\n\n" + item.Content})
	require.NoError(t, err)
	hits, err := f.vs.Search(ctx, port.VectorQuery{
		Vector: vecs[0], Model: "fake-model", Limit: 5,
	})
	require.NoError(t, err)
	require.Len(t, hits, 1)
	assert.Equal(t, item.ID, hits[0].ID)

	// any_embedding flag flipped to 1 on the persisted item
	got, _ := f.repo.Get(ctx, item.ID)
	assert.Equal(t, 1, got.AnyEmbedding, "any_embedding must be set after successful embed")
}

func TestEmbedService_IdempotentSecondCallIsNoop(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	ctx := context.Background()
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "x"
	require.NoError(t, f.repo.Create(ctx, item))

	require.NoError(t, f.svc.Embed(ctx, item.ID, item.Title, item.Content))
	// Second embed call should not error and item should remain embedded.
	require.NoError(t, f.svc.Embed(ctx, item.ID, item.Title, item.Content))

	got, _ := f.repo.Get(ctx, item.ID)
	assert.Equal(t, 1, got.AnyEmbedding)

	// Still exactly one vector (Put is DELETE+INSERT inside a tx).
	vecs, _ := f.emb.Embed(ctx, []string{item.Title + "\n\n" + item.Content})
	hits, _ := f.vs.Search(ctx, port.VectorQuery{
		Vector: vecs[0], Model: "fake-model", Limit: 5,
	})
	assert.Len(t, hits, 1)
}

func TestEmbedService_DoesNotPanicOnRepoMissingItem(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()
	// The item doesn't exist in the repo. Embed writes the vector
	// (VectorStore.Put doesn't check repo), then fails at Get+Update when
	// flipping any_embedding. The error must be returned, not panic.
	// Status row write also happens (it doesn't depend on repo.Get).
	err := f.svc.Embed(context.Background(), "nonexistent-id", "t", "c")
	require.Error(t, err)
}

// TestEmbedService_HydratesContentFromFileStore locks in the Plan 2b fix:
// when the caller passes empty content for an externalized item (Content
// cleared after fs.Put, ContentURI set), EmbedService hydrates the content
// from FileStore. Pre-2b this path produced a title-only embedding.
func TestEmbedService_HydratesContentFromFileStore(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "externalized"
	contentBytes := []byte("this content lives in the filestore not inline")
	uri, _, err := f.fs.Put(contentBytes, "text/plain")
	require.NoError(t, err)
	item.ContentURI = uri
	item.Content = "" // simulating post-externalization state
	require.NoError(t, f.repo.Create(context.Background(), item))

	// Capture what text the embedder received.
	var receivedTexts []string
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		receivedTexts = texts
		return [][]float32{make([]float32, 8)}, nil
	})

	// Pass empty content; EmbedService should hydrate from fs.
	require.NoError(t, f.svc.Embed(context.Background(), item.ID, item.Title, ""))

	require.Len(t, receivedTexts, 1)
	assert.Contains(t, receivedTexts[0], "externalized", "title is in embed text")
	assert.Contains(t, receivedTexts[0], "this content lives in the filestore",
		"hydrated content is in embed text (this would fail before 2b)")
}

// TestEmbedService_WritesStatusRowOnSuccess verifies the Plan 2b status-row
// policy: every successful embed writes status='done', attempts=1.
func TestEmbedService_WritesStatusRowOnSuccess(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "ok"
	require.NoError(t, f.repo.Create(context.Background(), item))

	require.NoError(t, f.svc.Embed(context.Background(), item.ID, item.Title, "body"))

	st, err := f.embRepo.GetStatus(context.Background(), item.ID, "fake-model")
	require.NoError(t, err)
	assert.Equal(t, "done", st.Status)
	assert.Equal(t, 1, st.Attempts)
}

// TestEmbedService_WritesStatusRowOnFailure verifies the failure path:
// embedder failure writes status='failed' with the error text.
func TestEmbedService_WritesStatusRowOnFailure(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = "fail"
	require.NoError(t, f.repo.Create(context.Background(), item))

	// Force embedder failure via the hook.
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		return nil, errors.New("ollama unreachable")
	})

	err := f.svc.Embed(context.Background(), item.ID, item.Title, "body")
	require.Error(t, err)

	st, getErr := f.embRepo.GetStatus(context.Background(), item.ID, "fake-model")
	require.NoError(t, getErr)
	assert.Equal(t, "failed", st.Status)
	assert.Contains(t, st.LastError, "ollama unreachable")
	assert.Equal(t, 1, st.Attempts)
}
