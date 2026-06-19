package cli

import (
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"uni-context/internal/domain"
	"uni-context/internal/port"
	"uni-context/internal/service"
)

var userCmd = &cobra.Command{
	Use:   "user",
	Short: "Manage personal-scope (user) knowledge",
}

var userNoteCmd = &cobra.Command{
	Use:   "note",
	Short: "Manage personal notes",
}

var (
	noteTitle string
	noteTags  []string
	noteLimit int
)

var userNoteAddCmd = &cobra.Command{
	Use:   "add [content|-]",
	Short: "Add a personal note. Pass - to read content from stdin.",
	Args:  cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		content, err := readContent(args)
		if err != nil {
			return err
		}
		a, cfg, err := loadApp()
		if err != nil {
			return err
		}
		defer a.DB.Close()

		id, err := a.Ingest.Create(cmd.Context(), inputFromFlags(
			domain.ScopeUser, domain.KindNote, domain.SourceManual,
			cfg.User.ID, "", noteTitle, content, noteTags,
		))
		if err != nil {
			return err
		}
		if flagJSON {
			printJSON(map[string]string{"id": id, "status": "added"})
		} else {
			fmt.Printf("added: %s\n", id)
		}
		return nil
	},
}

var userNoteListCmd = &cobra.Command{
	Use:   "list",
	Short: "List personal notes (newest first)",
	RunE: func(cmd *cobra.Command, args []string) error {
		a, cfg, err := loadApp()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if noteLimit <= 0 {
			noteLimit = 20
		}
		items, _, err := a.Repo.List(cmd.Context(), port.ItemFilter{
			Scopes:      []domain.Scope{domain.ScopeUser},
			OwnerUserID: cfg.User.ID,
			Kinds:       []domain.Kind{domain.KindNote},
			Limit:       noteLimit,
		})
		if err != nil {
			return err
		}
		if flagJSON {
			out := make([]map[string]any, 0, len(items))
			for _, it := range items {
				out = append(out, map[string]any{
					"id":         it.ID,
					"title":      it.Title,
					"tags":       it.Tags,
					"created_at": it.CreatedAt.Format(time.RFC3339),
				})
			}
			printJSON(out)
			return nil
		}
		if len(items) == 0 {
			fmt.Println("(no notes)")
			return nil
		}
		for _, it := range items {
			tags := strings.Join(it.Tags, ",")
			fmt.Printf("%s  %s  [%s]\n", it.ID, it.Title, tags)
		}
		return nil
	},
}

var userNoteGetCmd = &cobra.Command{
	Use:   "get <id>",
	Short: "Show a single note",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadApp()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		item, err := a.Repo.Get(cmd.Context(), args[0])
		if err != nil {
			return err
		}
		content := item.Content
		if content == "" && item.ContentURI != "" {
			data, err := a.FS.Get(item.ContentURI)
			if err != nil {
				return fmt.Errorf("load external content: %w", err)
			}
			content = string(data)
		}
		if flagJSON {
			printJSON(map[string]any{
				"id":         item.ID,
				"title":      item.Title,
				"summary":    item.Summary,
				"content":    content,
				"tags":       item.Tags,
				"created_at": item.CreatedAt.Format(time.RFC3339),
				"updated_at": item.UpdatedAt.Format(time.RFC3339),
			})
			return nil
		}
		fmt.Printf("id:    %s\n", item.ID)
		fmt.Printf("title: %s\n", item.Title)
		fmt.Printf("tags:  %s\n", strings.Join(item.Tags, ", "))
		fmt.Println("---")
		fmt.Println(content)
		return nil
	},
}

var userNoteDeleteCmd = &cobra.Command{
	Use:   "delete <id>",
	Short: "Delete a note",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		a, _, err := loadApp()
		if err != nil {
			return err
		}
		defer a.DB.Close()
		if err := a.Repo.Delete(cmd.Context(), args[0]); err != nil {
			return err
		}
		if flagJSON {
			printJSON(map[string]string{"id": args[0], "status": "deleted"})
		} else {
			fmt.Printf("deleted: %s\n", args[0])
		}
		return nil
	},
}

func init() {
	userNoteAddCmd.Flags().StringVar(&noteTitle, "title", "", "note title")
	userNoteAddCmd.Flags().StringSliceVar(&noteTags, "tag", nil, "tags (comma-separated or repeat)")
	userNoteListCmd.Flags().IntVar(&noteLimit, "limit", 20, "max items to return")

	userNoteCmd.AddCommand(userNoteAddCmd, userNoteListCmd, userNoteGetCmd, userNoteDeleteCmd)
	userCmd.AddCommand(userNoteCmd)
	rootCmd.AddCommand(userCmd)
}

func readContent(args []string) (string, error) {
	if len(args) == 0 || args[0] != "-" {
		if len(args) == 0 {
			return "", fmt.Errorf("content required (positional arg or - for stdin)")
		}
		return args[0], nil
	}
	data, err := io.ReadAll(os.Stdin)
	if err != nil {
		return "", fmt.Errorf("read stdin: %w", err)
	}
	return string(data), nil
}

func inputFromFlags(scope domain.Scope, kind domain.Kind, source domain.Source,
	owner, project, title, content string, tags []string,
) service.Input {
	return service.Input{
		Scope:       scope,
		Kind:        kind,
		Source:      source,
		OwnerUserID: owner,
		ProjectID:   project,
		Title:       title,
		Content:     content,
		Tags:        tags,
	}
}
