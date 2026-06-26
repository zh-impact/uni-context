package service

import (
	"context"

	"uni-context/internal/port"
)

// DiagnosticService powers the `doctor` command. It owns the schema-version
// lookup (previously a raw `a.DB.QueryRow(...)` in the CLI) and the embedder
// health check (previously inline `a.Embedder.Embed` / `.Model()` calls).
// Routing these through a service means the inbound layer has no direct
// dependency on *sql.DB or port.Embedder, mirroring how ItemService owns
// the get/list/delete path.
type DiagnosticService struct {
	schema   port.SchemaMeta
	embedder port.Embedder // nil = Plan 1 (disabled); no PingEmbedder probe
}

// NewDiagnosticService wires the schema-meta reader and the optional
// embedder. embedder may be nil — PingEmbedder reports disabled in that
// case rather than attempting an Embed call.
func NewDiagnosticService(schema port.SchemaMeta, embedder port.Embedder) *DiagnosticService {
	return &DiagnosticService{schema: schema, embedder: embedder}
}

// SchemaVersion returns the migration version string from schema_meta.
// Errors propagate unwrapped so callers can distinguish a missing
// schema_meta table (uninitialized DB) from other failures.
func (s *DiagnosticService) SchemaVersion(ctx context.Context) (string, error) {
	return s.schema.Version(ctx)
}

// PingEmbedder exercises the live embedder with a one-token embed to
// surface transient failures (Ollama down, wrong base URL, auth reject)
// before they bite a real search. Returns:
//   - (zero, false, nil) when no embedder is wired (Plan 1). The CLI
//     uses enabled=false to print "disabled" rather than "FAIL".
//   - (ModelInfo, true, nil) when the embedder answered the ping. The
//     CLI prints "<slug>, <dim>-dim".
//   - (zero, true, err) when the embedder exists but the ping failed.
//     enabled stays true so the CLI prints "FAIL (...)", not "disabled".
//     Model() is intentionally NOT called on failure — matches the
//     previous inline doctor code and avoids masking the embed error
//     with a stale model label.
func (s *DiagnosticService) PingEmbedder(ctx context.Context) (port.ModelInfo, bool, error) {
	if s.embedder == nil {
		return port.ModelInfo{}, false, nil
	}
	if _, err := s.embedder.Embed(ctx, []string{"ping"}); err != nil {
		return port.ModelInfo{}, true, err
	}
	return s.embedder.Model(), true, nil
}
