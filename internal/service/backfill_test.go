package service

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/domain"
)

func TestBackfillService_ProcessesOnlyUnembeddedItems(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	// 3 items: A and B unembedded (any_embedding=0), C already embedded.
	itemA := makeItemForBackfill(t, f, "alpha", "content A")
	itemB := makeItemForBackfill(t, f, "beta", "content B")
	itemC := makeItemForBackfill(t, f, "gamma", "content C")
	// Mark C as already embedded
	require.NoError(t, f.svc.Embed(context.Background(), itemC, "gamma", "content C"))

	svc := NewBackfillService(f.repo, f.svc)
	report, err := svc.Run(context.Background(), 0, false)
	require.NoError(t, err)

	assert.Equal(t, 2, report.Embedded, "only A and B embedded; C excluded by filter")
	assert.Equal(t, 0, report.Failed)

	// Verify A and B now have status='done'
	for _, id := range []string{itemA, itemB} {
		st, err := f.embRepo.GetStatus(context.Background(), id, "fake-model")
		require.NoError(t, err)
		assert.Equal(t, "done", st.Status, "item %s should be embedded now", id)
	}
}

func TestBackfillService_DryRunDoesNotEmbed(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	itemA := makeItemForBackfill(t, f, "alpha", "content A")

	svc := NewBackfillService(f.repo, f.svc)
	report, err := svc.Run(context.Background(), 0, true) // dryRun=true
	require.NoError(t, err)

	assert.Equal(t, 0, report.Embedded, "dry run does not embed")
	assert.Equal(t, 1, report.Scanned, "dry run counts candidates")

	// Item A still has no embedding
	_, err = f.embRepo.GetStatus(context.Background(), itemA, "fake-model")
	require.Error(t, err, "no status row written during dry run")
}

func TestBackfillService_LimitHonored(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	for _, title := range []string{"a", "b", "c", "d", "e"} {
		makeItemForBackfill(t, f, title, "content "+title)
	}

	svc := NewBackfillService(f.repo, f.svc)
	report, err := svc.Run(context.Background(), 3, false) // limit=3
	require.NoError(t, err)

	assert.Equal(t, 3, report.Embedded, "limit caps the run")
	assert.Equal(t, 3, report.Scanned)
}

func TestBackfillService_ContinuesOnEmbedFailure(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	_ = makeItemForBackfill(t, f, "alpha", "A")
	itemB := makeItemForBackfill(t, f, "beta", "B")
	_ = makeItemForBackfill(t, f, "gamma", "C")

	// Fail ONLY when embedding item B (by title match)
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		if len(texts) > 0 && strings.Contains(texts[0], "beta") {
			return nil, errors.New("simulated failure on beta")
		}
		return [][]float32{make([]float32, 8)}, nil
	})

	svc := NewBackfillService(f.repo, f.svc)
	report, err := svc.Run(context.Background(), 0, false)
	require.NoError(t, err, "Run itself does not fail on per-item errors")

	assert.Equal(t, 2, report.Embedded, "A and C embedded")
	assert.Equal(t, 1, report.Failed, "B failed")
	require.Len(t, report.Failures, 1)
	assert.Equal(t, itemB, report.Failures[0].ItemID)
}

// helper
func makeItemForBackfill(t *testing.T, f *embedFixture, title, content string) string {
	t.Helper()
	item, _ := domain.NewContextItem(domain.ScopeUser, domain.KindNote, domain.SourceManual,
		domain.NewItemParams{OwnerUserID: "u"})
	item.Title = title
	item.Content = content
	require.NoError(t, f.repo.Create(context.Background(), item))
	return item.ID
}
