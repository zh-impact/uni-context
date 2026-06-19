package service

import (
	"context"
	"fmt"
	"sync"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

type fakeRepo struct {
	mu        sync.Mutex
	items     map[string]domain.ContextItem
	createErr error // injectable; if set, Create returns this
}

func newFakeRepo() *fakeRepo {
	return &fakeRepo{items: map[string]domain.ContextItem{}}
}

func (r *fakeRepo) Create(_ context.Context, item domain.ContextItem) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.createErr != nil {
		return r.createErr
	}
	if _, exists := r.items[item.ID]; exists {
		return fmt.Errorf("duplicate id")
	}
	r.items[item.ID] = item
	return nil
}

func (r *fakeRepo) Get(_ context.Context, id string) (domain.ContextItem, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	item, ok := r.items[id]
	if !ok {
		return domain.ContextItem{}, fmt.Errorf("%w: %s", domain.ErrNotFound, id)
	}
	return item, nil
}

func (r *fakeRepo) Update(_ context.Context, item domain.ContextItem) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.items[item.ID]; !ok {
		return fmt.Errorf("%w: %s", domain.ErrNotFound, item.ID)
	}
	r.items[item.ID] = item
	return nil
}

func (r *fakeRepo) Delete(_ context.Context, id string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.items[id]; !ok {
		return fmt.Errorf("%w: %s", domain.ErrNotFound, id)
	}
	delete(r.items, id)
	return nil
}

func (r *fakeRepo) List(_ context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]domain.ContextItem, 0, len(r.items))
	for _, it := range r.items {
		out = append(out, it)
	}
	return out, "", nil
}

func (r *fakeRepo) NextCursor(_ domain.ContextItem) string { return "" }
