package cli

import (
	"fmt"
	"strings"
	"time"

	"uni-context/internal/domain"
	"uni-context/internal/service"

	"github.com/spf13/cobra"
)

var (
	searchScopes []string
	searchKinds  []string
	searchLimit  int
	searchMode   string
)

var searchCmd = &cobra.Command{
	Use:   "search <query>",
	Short: "Search across all scopes (Plan 2a: fts-only | hybrid)",
	Args:  cobra.MinimumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		query := strings.Join(args, " ")
		mode := normalizeSearchMode(searchMode)
		if err := validateSearchMode(mode); err != nil {
			return err
		}

		a, _, err := loadApp()
		if err != nil {
			return err
		}
		defer a.Close()

		if searchLimit <= 0 {
			searchLimit = 20
		}

		req, err := serviceSearchReq(query, mode)
		if err != nil {
			return err
		}
		resp, err := a.Search.Search(cmd.Context(), req)
		if err != nil {
			return err
		}

		if flagJSON {
			out := make([]map[string]any, 0, len(resp.Results))
			for _, r := range resp.Results {
				out = append(out, map[string]any{
					"id":         r.Item.ID,
					"title":      r.Item.Title,
					"scope":      string(r.Item.Scope),
					"kind":       string(r.Item.Kind),
					"score":      r.Score,
					"snippet":    r.Snippet,
					"matched_by": r.MatchedBy,
					"tags":       r.Item.Tags,
					"created_at": r.Item.CreatedAt.Format(time.RFC3339),
				})
			}
			printJSON(map[string]any{
				"results": out,
				"total":   resp.Total,
				"mode":    mode,
			})
			return nil
		}

		if len(resp.Results) == 0 {
			fmt.Println("(no matches)")
			return nil
		}
		for _, r := range resp.Results {
			fmt.Printf("[%s]  %s\n  scope=%s kind=%s score=%.3f (matched: %s)\n  %s\n\n",
				r.Item.ID[:8], r.Item.Title,
				r.Item.Scope, r.Item.Kind, r.Score, strings.Join(r.MatchedBy, "+"),
				r.Snippet)
		}
		return nil
	},
}

func serviceSearchReq(query, mode string) (service.SearchRequest, error) {
	scopes, err := parseScopes(searchScopes)
	if err != nil {
		return service.SearchRequest{}, err
	}
	kinds, err := parseKinds(searchKinds)
	if err != nil {
		return service.SearchRequest{}, err
	}
	return service.SearchRequest{
		Query:  query,
		Scopes: scopes,
		Kinds:  kinds,
		Limit:  searchLimit,
		Mode:   service.SearchMode(mode),
	}, nil
}

// normalizeSearchMode maps the empty string to the Plan 2a default.
// Non-default values are returned verbatim; validity is checked by
// validateSearchMode.
func normalizeSearchMode(mode string) string {
	if mode == "" {
		return "fts-only"
	}
	return mode
}

// validateSearchMode enforces the Plan 2a allow-list of search modes.
// Anything outside {"fts-only","hybrid"} yields an error that echoes
// the bad value back to the user.
func validateSearchMode(mode string) error {
	switch mode {
	case "fts-only", "hybrid":
		return nil
	default:
		return fmt.Errorf("--mode %q not supported (Plan 2a: fts-only | hybrid)", mode)
	}
}

var validScopes = map[domain.Scope]bool{
	domain.ScopeUser:    true,
	domain.ScopeProject: true,
	domain.ScopeGlobal:  true,
}

var validKinds = map[domain.Kind]bool{
	domain.KindNote:            true,
	domain.KindExcerpt:         true,
	domain.KindLink:            true,
	domain.KindDoc:             true,
	domain.KindConversationMsg: true,
	domain.KindMemory:          true,
	domain.KindFile:            true,
}

func parseScopes(in []string) ([]domain.Scope, error) {
	out := make([]domain.Scope, 0, len(in))
	for _, s := range in {
		scope := domain.Scope(s)
		if !validScopes[scope] {
			return nil, fmt.Errorf("invalid scope %q (valid: user, project, global)", s)
		}
		out = append(out, scope)
	}
	return out, nil
}

func parseKinds(in []string) ([]domain.Kind, error) {
	out := make([]domain.Kind, 0, len(in))
	for _, s := range in {
		kind := domain.Kind(s)
		if !validKinds[kind] {
			return nil, fmt.Errorf("invalid kind %q (valid: note, excerpt, link, doc, conversation_msg, memory, file)", s)
		}
		out = append(out, kind)
	}
	return out, nil
}

func init() {
	searchCmd.Flags().StringSliceVar(&searchScopes, "scope", nil, "filter by scope (user,project,global)")
	searchCmd.Flags().StringSliceVar(&searchKinds, "kind", nil, "filter by kind (note,doc,memory,...)")
	searchCmd.Flags().IntVar(&searchLimit, "limit", 20, "max results")
	searchCmd.Flags().StringVar(&searchMode, "mode", "fts-only", "search mode (Plan 2a: fts-only | hybrid)")
	rootCmd.AddCommand(searchCmd)
}
