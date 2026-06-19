package cli

import (
	"strings"
	"testing"

	"uni-context/internal/domain"
)

// formatListItem governs the `user note list` row format. The most important
// branch is the title-empty fallback: when a user runs `unictx user note add
// <content>` without --title (a common case), the row must still show a
// preview of the inline content so the user sees something useful. These
// tests lock in the four documented behaviors so future changes can't
// silently regress the UX fix.
func TestFormatListItem(t *testing.T) {
	base := domain.ContextItem{ID: "abc123"}

	tests := []struct {
		name  string
		item  domain.ContextItem
		want  string
	}{
		{
			name: "title present wins over content",
			item: domain.ContextItem{ID: "abc123", Title: "My Note", Content: "ignored body"},
			want: "abc123  My Note  []",
		},
		{
			name: "title empty with short content previews verbatim",
			item: domain.ContextItem{ID: "abc123", Title: "", Content: "short body"},
			want: "abc123  short body  []",
		},
		{
			name: "title empty with long content truncates at 50 runes",
			item: domain.ContextItem{ID: "abc123", Title: "", Content: strings.Repeat("字", 80)},
			want: "abc123  " + strings.Repeat("字", 50) + "…  []",
		},
		{
			name: "title and content empty with ContentURI shows externalized",
			item: domain.ContextItem{ID: "abc123", Title: "", Content: "", ContentURI: "sha256://abc"},
			want: "abc123  (externalized)  []",
		},
		{
			name: "all empty shows no content placeholder",
			item: base,
			want: "abc123  (no content)  []",
		},
		{
			name: "tags joined with comma",
			item: domain.ContextItem{ID: "abc123", Title: "T", Tags: []string{"go", "deploy"}},
			want: "abc123  T  [go,deploy]",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := formatListItem(tc.item)
			if got != tc.want {
				t.Errorf("formatListItem: got %q, want %q", got, tc.want)
			}
		})
	}
}

// previewRunes returns the first n runes of s, appending an ellipsis when
// truncation occurred. CJK safety is verified via a unicode test case.
func TestPreviewRunes(t *testing.T) {
	tests := []struct {
		s    string
		n    int
		want string
	}{
		{"abc", 5, "abc"},          // shorter than n: verbatim
		{"abcdef", 3, "abc…"},      // ascii truncate
		{"你好世界", 2, "你好…"},     // CJK truncate, no byte-misalignment
		{"", 5, ""},                // empty input
		{"abc", 3, "abc"},          // exact-length: no ellipsis
	}
	for _, tc := range tests {
		got := previewRunes(tc.s, tc.n)
		if got != tc.want {
			t.Errorf("previewRunes(%q, %d): got %q, want %q", tc.s, tc.n, got, tc.want)
		}
	}
}
