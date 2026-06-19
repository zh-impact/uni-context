package cli

import (
	"fmt"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"uni-context/internal/domain"
	"uni-context/internal/service"
)

var (
	searchScopes []string
	searchKinds  []string
	searchLimit  int
	searchMode   string
)

var searchCmd = &cobra.Command{
	Use:   "search <query>",
	Short: "Search across all scopes (Plan 1: FTS-only; vector mode arrives in Plan 2)",
	Args:  cobra.MinimumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		query := strings.Join(args, " ")
		mode := searchMode
		if mode == "" {
			mode = "fts-only" // Plan 1 default; will become "hybrid" in Plan 2
		}
		if mode != "fts-only" {
			return fmt.Errorf("--mode %q not supported in Plan 1 (only 'fts-only')", mode)
		}

		a, _, err := loadApp()
		if err != nil {
			return err
		}
		defer a.DB.Close()

		if searchLimit <= 0 {
			searchLimit = 20
		}

		resp, err := a.Search.Search(cmd.Context(), serviceSearchReq(query))
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
			fmt.Printf("[%s]  %s\n  scope=%s kind=%s score=%.3f\n  %s\n\n",
				r.Item.ID[:8], r.Item.Title,
				r.Item.Scope, r.Item.Kind, r.Score, r.Snippet)
		}
		return nil
	},
}

func serviceSearchReq(query string) service.SearchRequest {
	return service.SearchRequest{
		Query:  query,
		Scopes: parseScopes(searchScopes),
		Kinds:  parseKinds(searchKinds),
		Limit:  searchLimit,
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
	searchCmd.Flags().StringVar(&searchMode, "mode", "fts-only", "search mode (Plan 1: fts-only)")
	rootCmd.AddCommand(searchCmd)
}
