package service

import (
	"context"
	"errors"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/port"
)

// recordingRegistry is a port.ModelRegistry double that captures the
// arguments of each mutating method and returns the canned error.
// Read methods (List/Get/GetActive) return canned data. Used to verify
// ModelService forwards the right args to the right methods.
type recordingRegistry struct {
	registered port.ModelSpec
	removed    string
	setDefault string
	listErr    error
	listOut    []port.ModelDescriptor
}

func (r *recordingRegistry) List(_ context.Context) ([]port.ModelDescriptor, error) {
	return r.listOut, r.listErr
}
func (r *recordingRegistry) GetActive(_ context.Context) (port.ModelDescriptor, error) {
	panic("unexpected GetActive")
}
func (r *recordingRegistry) Get(_ context.Context, _ string) (port.ModelDescriptor, error) {
	panic("unexpected Get")
}
func (r *recordingRegistry) Register(_ context.Context, spec port.ModelSpec) error {
	r.registered = spec
	return nil
}
func (r *recordingRegistry) UpdateConfig(_ context.Context, _, _, _, _ string) error {
	panic("unexpected UpdateConfig")
}
func (r *recordingRegistry) SetDefault(_ context.Context, slug string) error {
	r.setDefault = slug
	return nil
}
func (r *recordingRegistry) Remove(_ context.Context, slug string) error {
	r.removed = slug
	return nil
}

// recordingEmbRepo captures ListForItem calls. The other methods are
// unused by ModelService and panic if hit.
type recordingEmbRepo struct {
	listItemArg string
	listOut     []port.EmbeddingStatus
	listErr     error
}

func (r *recordingEmbRepo) UpsertStatus(_ context.Context, _, _, _, _ string) error {
	panic("unexpected UpsertStatus")
}
func (r *recordingEmbRepo) GetStatus(_ context.Context, _, _ string) (port.EmbeddingStatus, error) {
	panic("unexpected GetStatus")
}
func (r *recordingEmbRepo) ListFailed(_ context.Context, _ int) ([]port.EmbeddingStatus, error) {
	panic("unexpected ListFailed")
}
func (r *recordingEmbRepo) ListForItem(_ context.Context, itemID string) ([]port.EmbeddingStatus, error) {
	r.listItemArg = itemID
	return r.listOut, r.listErr
}

// TestModelService_AddModel_DelegatesToRegistry: ModelService is a thin
// pass-through — AddModel forwards the spec verbatim to Registry.Register.
// The value of this service is the App boundary, not added logic.
func TestModelService_AddModel_DelegatesToRegistry(t *testing.T) {
	reg := &recordingRegistry{}
	svc := NewModelService(reg, nil)

	spec := port.ModelSpec{Slug: "bge-m3", Provider: "ollama", Dimension: 1024}
	require.NoError(t, svc.AddModel(context.Background(), spec))
	assert.Equal(t, spec, reg.registered,
		"AddModel must forward the spec unchanged to Registry.Register")
}

// TestModelService_ListModels_DelegatesToRegistry: ListModels returns the
// registry's rows verbatim and propagates its error. The CLI's tabwriter
// formatting stays in the inbound layer.
func TestModelService_ListModels_DelegatesToRegistry(t *testing.T) {
	canned := []port.ModelDescriptor{{Slug: "a"}, {Slug: "b"}}
	reg := &recordingRegistry{listOut: canned}
	svc := NewModelService(reg, nil)

	got, err := svc.ListModels(context.Background())
	require.NoError(t, err)
	assert.Equal(t, canned, got)

	// Error propagation
	reg.listErr = errors.New("db unavailable")
	_, err = svc.ListModels(context.Background())
	require.Error(t, err)
}

// TestModelService_RemoveModel_DelegatesToRegistry: RemoveModel forwards
// the slug. Errors from Remove (not-found, default refusal, shared-table
// refusal) propagate to the CLI unchanged.
func TestModelService_RemoveModel_DelegatesToRegistry(t *testing.T) {
	reg := &recordingRegistry{}
	svc := NewModelService(reg, nil)

	require.NoError(t, svc.RemoveModel(context.Background(), "stale-model"))
	assert.Equal(t, "stale-model", reg.removed)
}

// TestModelService_SwitchModel_DelegatesToRegistry: SwitchModel forwards
// to SetDefault. The post-switch "run reembed" reminder stays in the CLI —
// it's a UI concern, not a service-layer invariant.
func TestModelService_SwitchModel_DelegatesToRegistry(t *testing.T) {
	reg := &recordingRegistry{}
	svc := NewModelService(reg, nil)

	require.NoError(t, svc.SwitchModel(context.Background(), "bge-m3"))
	assert.Equal(t, "bge-m3", reg.setDefault)
}

// TestModelService_ItemEmbeddingStatus_DelegatesToEmbRepo: the status
// command's data comes from EmbeddingRepo.ListForItem. ModelService just
// forwards the itemID and returns whatever the repo produced.
func TestModelService_ItemEmbeddingStatus_DelegatesToEmbRepo(t *testing.T) {
	canned := []port.EmbeddingStatus{{ItemID: "i1", ModelSlug: "bge-m3", Status: "done"}}
	emb := &recordingEmbRepo{listOut: canned}
	svc := NewModelService(nil, emb)

	got, err := svc.ItemEmbeddingStatus(context.Background(), "i1")
	require.NoError(t, err)
	assert.Equal(t, canned, got)
	assert.Equal(t, "i1", emb.listItemArg,
		"ItemEmbeddingStatus must forward the itemID to ListForItem")
}
