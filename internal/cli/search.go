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
		defer a.DB.Close()

		if searchLimit <= 0 {
			searchLimit = 20
		}

		resp, err := a.Search.Search(cmd.Context(), serviceSearchReq(query, mode))
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

func serviceSearchReq(query, mode string) service.SearchRequest {
	return service.SearchRequest{
		Query:  query,
		Scopes: parseScopes(searchScopes),
		Kinds:  parseKinds(searchKinds),
		Limit:  searchLimit,
		Mode:   service.SearchMode(mode),
	}
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

func parseScopes(in []string) []domain.Scope {
	out := make([]domain.Scope, 0, len(in))
	for _, s := range in {
		out = append(out, domain.Scope(s))
	}
	return out
}

func parseKinds(in []string) []domain.Kind {
	out := make([]domain.Kind, 0, len(in))
	for _, s := range in {
		out = append(out, domain.Kind(s))
	}
	return out
}

func init() {
	searchCmd.Flags().StringSliceVar(&searchScopes, "scope", nil, "filter by scope (user,project,global)")
	searchCmd.Flags().StringSliceVar(&searchKinds, "kind", nil, "filter by kind (note,doc,memory,...)")
	searchCmd.Flags().IntVar(&searchLimit, "limit", 20, "max results")
	searchCmd.Flags().StringVar(&searchMode, "mode", "fts-only", "search mode (Plan 2a: fts-only | hybrid)")
	rootCmd.AddCommand(searchCmd)
}
