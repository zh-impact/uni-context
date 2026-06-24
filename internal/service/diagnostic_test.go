package service

import (
	"context"
	"errors"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"uni-context/internal/adapter/embedder/fake"
	"uni-context/internal/port"
)

// stubSchemaMeta is a port.SchemaMeta double. version is returned
// verbatim; err, when non-nil, is returned in place of version.
type stubSchemaMeta struct {
	version string
	err     error
}

func (s stubSchemaMeta) Version(_ context.Context) (string, error) {
	return s.version, s.err
}

// TestDiagnosticService_SchemaVersion_ReturnsRepoValue: the service is a
// thin pass-through over SchemaMeta.Version — the value the repo returns
// is what the CLI prints, unchanged.
func TestDiagnosticService_SchemaVersion_ReturnsRepoValue(t *testing.T) {
	svc := NewDiagnosticService(stubSchemaMeta{version: "4"}, nil)

	v, err := svc.SchemaVersion(context.Background())
	require.NoError(t, err)
	assert.Equal(t, "4", v, "service must return the repo's schema_version verbatim")
}

// TestDiagnosticService_SchemaVersion_PropagatesRepoError: a failing
// schema_meta read (e.g. fresh DB where migration hasn't run yet) must
// surface as an error, not be swallowed — the CLI uses this to abort
// `doctor` so the user knows their DB is uninitialized.
func TestDiagnosticService_SchemaVersion_PropagatesRepoError(t *testing.T) {
	svc := NewDiagnosticService(stubSchemaMeta{err: errors.New("no such table: schema_meta")}, nil)

	_, err := svc.SchemaVersion(context.Background())
	require.Error(t, err)
	assert.Contains(t, err.Error(), "no such table")
}

// TestDiagnosticService_PingEmbedder_DisabledReturnsFalseNoError: when no
// embedder is wired (Plan 1), PingEmbedder reports disabled=false rather
// than attempting an Embed call. The CLI uses `enabled` to decide whether
// to print "disabled" or probe for health.
func TestDiagnosticService_PingEmbedder_DisabledReturnsFalseNoError(t *testing.T) {
	svc := NewDiagnosticService(stubSchemaMeta{}, nil) // nil embedder = Plan 1

	info, enabled, err := svc.PingEmbedder(context.Background())
	require.NoError(t, err, "disabled embedder must not error")
	assert.False(t, enabled, "disabled embedder reports enabled=false")
	assert.Equal(t, port.ModelInfo{}, info, "no model info when disabled")
}

// TestDiagnosticService_PingEmbedder_HealthyReturnsModelTrueNil: a working
// embedder returns (ModelInfo, enabled=true, nil) so the CLI can print
// "<slug>, <dim>-dim". The one-token Embed is exercised so transient
// failures (Ollama down, wrong base URL) surface here instead of during
// real search.
func TestDiagnosticService_PingEmbedder_HealthyReturnsModelTrueNil(t *testing.T) {
	emb := fake.New("fake-model", 8)
	svc := NewDiagnosticService(stubSchemaMeta{}, emb)

	info, enabled, err := svc.PingEmbedder(context.Background())
	require.NoError(t, err)
	assert.True(t, enabled)
	assert.Equal(t, "fake-model", info.Slug)
	assert.Equal(t, 8, info.Dimension)
}

// TestDiagnosticService_PingEmbedder_FailingReturnsTrueAndError: when
// the embedder errors on the ping, PingEmbedder must surface the error
// AND keep enabled=true so the CLI prints "FAIL (...)" rather than the
// Plan 1 "disabled" line. Model info is zero because Model() is only
// called on success (matches the previous inline doctor code).
func TestDiagnosticService_PingEmbedder_FailingReturnsTrueAndError(t *testing.T) {
	emb := fake.New("fake-model", 8)
	emb.SetEmbedHook(func([]string) ([][]float32, error) {
		return nil, errors.New("connection refused")
	})
	svc := NewDiagnosticService(stubSchemaMeta{}, emb)

	info, enabled, err := svc.PingEmbedder(context.Background())
	require.Error(t, err)
	assert.Contains(t, err.Error(), "connection refused")
	assert.True(t, enabled, "failed embedder is still enabled — the CLI prints FAIL, not disabled")
	assert.Equal(t, port.ModelInfo{}, info, "no model info on failure (Model() is success-only)")
}
