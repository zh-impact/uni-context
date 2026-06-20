package service

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestWorkerService_RetriesFailedEmbeddings(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	// Three items, all failed on first embed attempt.
	itemA := makeItemForBackfill(t, f, "alpha", "A")
	itemB := makeItemForBackfill(t, f, "beta", "B")
	itemC := makeItemForBackfill(t, f, "gamma", "C")

	// Initial failed attempts — simulate via direct Embed calls with
	// a failing hook.
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		return nil, errors.New("transient")
	})
	for _, id := range []string{itemA, itemB, itemC} {
		_ = f.svc.Embed(context.Background(), id, "title", "content")
	}

	// Verify all 3 are 'failed' with attempts=1
	for _, id := range []string{itemA, itemB, itemC} {
		st, _ := f.embRepo.GetStatus(context.Background(), id, "fake-model")
		require.Equal(t, "failed", st.Status)
		require.Equal(t, 1, st.Attempts)
	}

	// Now flip hook to succeed; run worker for ONE iteration.
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		return [][]float32{make([]float32, 8)}, nil
	})

	// EmbedService doesn't know about BackfillService's helper; use repo
	// to fetch title/content for the worker. The WorkerService will
	// fetch internally.
	svc := NewWorkerService(f.repo, f.embRepo, f.svc)

	// RunOneIteration exposes single-pass semantics for testing.
	processed, err := svc.RunOneIteration(context.Background())
	require.NoError(t, err)
	assert.Equal(t, 3, processed, "all 3 failures retried")

	// All 3 should now be 'done' with attempts=2
	for _, id := range []string{itemA, itemB, itemC} {
		st, _ := f.embRepo.GetStatus(context.Background(), id, "fake-model")
		assert.Equal(t, "done", st.Status)
		assert.Equal(t, 2, st.Attempts, "attempts incremented to 2")
	}
}

func TestWorkerService_NoFailures_ReturnsZero(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	svc := NewWorkerService(f.repo, f.embRepo, f.svc)
	processed, err := svc.RunOneIteration(context.Background())
	require.NoError(t, err)
	assert.Equal(t, 0, processed, "nothing to retry")
}

func TestWorkerService_Run_ExitsOnContextCancel(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	svc := NewWorkerService(f.repo, f.embRepo, f.svc)
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // pre-cancelled

	err := svc.Run(ctx, 10*time.Millisecond)
	require.Error(t, err)
	assert.ErrorIs(t, err, context.Canceled)
}

func TestWorkerService_PartialFailure_KeepsItemInQueue(t *testing.T) {
	f, cleanup := newEmbedFixture(t)
	defer cleanup()

	// itemFail always fails; itemSucceed succeeds. After one iteration,
	// itemFail stays 'failed' (attempts++), itemSucceed flips to 'done'.
	itemFail := makeItemForBackfill(t, f, "fail-title", "content F")
	itemSucceed := makeItemForBackfill(t, f, "ok-title", "content S")

	// Initial failures
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		return nil, errors.New("init fail")
	})
	_ = f.svc.Embed(context.Background(), itemFail, "fail-title", "content F")
	_ = f.svc.Embed(context.Background(), itemSucceed, "ok-title", "content S")

	// Mixed hook: succeed for itemSucceed, fail for itemFail
	f.emb.SetEmbedHook(func(texts []string) ([][]float32, error) {
		if len(texts) > 0 && strings.Contains(texts[0], "ok-title") {
			return [][]float32{make([]float32, 8)}, nil
		}
		return nil, errors.New("persistent")
	})

	svc := NewWorkerService(f.repo, f.embRepo, f.svc)
	processed, err := svc.RunOneIteration(context.Background())
	require.NoError(t, err)
	assert.Equal(t, 2, processed)

	stFail, _ := f.embRepo.GetStatus(context.Background(), itemFail, "fake-model")
	assert.Equal(t, "failed", stFail.Status)
	assert.Equal(t, 2, stFail.Attempts)

	stOk, _ := f.embRepo.GetStatus(context.Background(), itemSucceed, "fake-model")
	assert.Equal(t, "done", stOk.Status)
	assert.Equal(t, 2, stOk.Attempts)
}
