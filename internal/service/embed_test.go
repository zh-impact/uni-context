package service

import (
	"context"
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
	svc := NewEmbedService(emb, vs, repo)
	cleanup := func() { _ = db.Close() }
	return &embedFixture{repo: repo, vs: vs, emb: emb, svc: svc}, cleanup
}

type embedFixture struct {
	repo port.ContextRepo
	vs   port.VectorStore
	emb  *fake.Embedder
	svc  *EmbedService
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
	err := f.svc.Embed(context.Background(), "nonexistent-id", "t", "c")
	require.Error(t, err)
}
