package cli

import (
	"strings"
	"testing"

	"uni-context/internal/domain"
	"uni-context/internal/service"
)

// TestSearchModeValidation exercises the --mode allow-list directly.
// Plan 2a accepts "fts-only" and "hybrid"; anything else must error
// with a message that echoes the bad value so users can see what they
// typed wrong. These tests lock in both branches so a future refactor
// cannot silently regress the validation.
func TestSearchModeValidation(t *testing.T) {
	// Save and restore global flag state so the test is isolated.
	prevMode, prevScopes, prevKinds, prevLimit := searchMode, searchScopes, searchKinds, searchLimit
	t.Cleanup(func() {
		searchMode, searchScopes, searchKinds, searchLimit = prevMode, prevScopes, prevKinds, prevLimit
	})

	cases := []struct {
		name    string
		mode    string
		wantErr string
	}{
		{name: "fts-only accepted", mode: "fts-only"},
		{name: "hybrid accepted", mode: "hybrid"},
		{name: "empty falls back to fts-only", mode: ""},
		{name: "garbage rejected", mode: "vector-only", wantErr: `--mode "vector-only" not supported`},
		{name: "case sensitive", mode: "Hybrid", wantErr: `--mode "Hybrid" not supported`},
		{name: "blank non-empty rejected", mode: " ", wantErr: `--mode " " not supported`},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			searchMode = tc.mode
			mode := searchMode
			if mode == "" {
				mode = "fts-only"
			}
			err := validateSearchMode(mode)
			switch {
			case tc.wantErr == "" && err != nil:
				t.Errorf("mode %q: unexpected error %v", tc.mode, err)
			case tc.wantErr != "" && err == nil:
				t.Errorf("mode %q: expected error containing %q, got nil", tc.mode, tc.wantErr)
			case tc.wantErr != "" && err != nil && !strings.Contains(err.Error(), tc.wantErr):
				t.Errorf("mode %q: error %q does not contain %q", tc.mode, err.Error(), tc.wantErr)
			}
		})
	}
}

// TestServiceSearchReqCarriesMode confirms the helper translates the
// user-facing string into a typed SearchMode and threads it into the
// request. This is the only glue between the CLI flag and the service
// layer — if it regresses, hybrid mode silently degrades to fts-only.
func TestServiceSearchReqCarriesMode(t *testing.T) {
	prevMode, prevScopes, prevKinds, prevLimit := searchMode, searchScopes, searchKinds, searchLimit
	t.Cleanup(func() {
		searchMode, searchScopes, searchKinds, searchLimit = prevMode, prevScopes, prevKinds, prevLimit
	})

	searchMode = "irrelevant" // mode is passed explicitly to the helper
	searchScopes = []string{"user", "project"}
	searchKinds = []string{"note"}
	searchLimit = 7

	req, err := serviceSearchReq("hello world", "hybrid")
	if err != nil {
		t.Fatalf("serviceSearchReq: %v", err)
	}

	if req.Query != "hello world" {
		t.Errorf("Query: got %q, want %q", req.Query, "hello world")
	}
	if req.Mode != service.SearchModeHybrid {
		t.Errorf("Mode: got %q, want %q", req.Mode, service.SearchModeHybrid)
	}
	if req.Limit != 7 {
		t.Errorf("Limit: got %d, want 7", req.Limit)
	}
	wantScopes := []domain.Scope{domain.Scope("user"), domain.Scope("project")}
	if len(req.Scopes) != len(wantScopes) {
		t.Fatalf("Scopes len: got %d, want %d", len(req.Scopes), len(wantScopes))
	}
	for i, s := range req.Scopes {
		if s != wantScopes[i] {
			t.Errorf("Scopes[%d]: got %q, want %q", i, s, wantScopes[i])
		}
	}
	wantKinds := []domain.Kind{domain.Kind("note")}
	if len(req.Kinds) != len(wantKinds) {
		t.Fatalf("Kinds len: got %d, want %d", len(req.Kinds), len(wantKinds))
	}
	for i, k := range req.Kinds {
		if k != wantKinds[i] {
			t.Errorf("Kinds[%d]: got %q, want %q", i, k, wantKinds[i])
		}
	}
}
