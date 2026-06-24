package cli

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"uni-context/internal/domain"
	"uni-context/internal/port"
	"uni-context/internal/service"

	"github.com/spf13/cobra"
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
	noteTitle      string
	noteTags       []string
	noteTagsFilter []string
	noteLimit      int
	noteFilePath   string
)

// userNoteLoadAppFn is the indirection that lets RunE tests swap in a
// stubbed *App without touching the real config/DB. Separate from
// embed.go's loadAppFn so each command file's tests are scoped to its
// own var. Defaults to the real loadApp in production.
var userNoteLoadAppFn = loadApp

var userNoteAddCmd = &cobra.Command{
	Use:   "add [content|-]",
	Short: "Add a personal note (positional arg, - for stdin, or --file <path>)",
	Long: `Add a personal note.

Content sources (mutually exclusive — pick one):
  positional arg    unictx user note add "hello world"
  -                 read from stdin (echo "hi" | unictx user note add -)
  --file <path>     import from a .txt or .md file (max 10 MB)

When --file is used without --title, the title defaults to the file's
basename with its extension stripped (weekly.md -> "weekly"). Markdown
files are tagged text/markdown so renderers can render them; other text
files default to text/plain.`,
	Args: cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		// Rule 0: --file "" (explicit empty) must not fall through to
		// readContent(args), which would surface the misleading
		// "content required (positional arg or - for stdin)".
		if cmd.Flags().Changed("file") && noteFilePath == "" {
			return fmt.Errorf("--file: path cannot be empty")
		}

		var content string
		var mime string
		sourceMeta := map[string]any{}
		if noteFilePath != "" {
			// File import path.
			if len(args) > 0 { // Rule 1: mutual exclusion
				return fmt.Errorf("cannot combine --file with positional content or -")
			}
			if err := validateFileImport(noteFilePath); err != nil { // Rules 2-4
				return err
			}
			data, err := os.ReadFile(noteFilePath)
			if err != nil {
				return fmt.Errorf("read file: %w", err)
			}
			content = string(data)
			mime = mimeForTextFile(noteFilePath)
			if !cmd.Flags().Changed("title") {
				noteTitle = deriveDefaultTitle(noteFilePath)
			}
			sourceMeta["original_filename"] = filepath.Base(noteFilePath)
		} else {
			// Existing path: positional arg OR "-" stdin. Unchanged.
			c, err := readContent(args)
			if err != nil {
				return err
			}
			content = c
		}

		a, cfg, err := userNoteLoadAppFn()
		if err != nil {
			return err
		}
		defer a.Close()

		id, err := a.Ingest.Create(cmd.Context(), service.Input{
			Scope:       domain.ScopeUser,
			Kind:        domain.KindNote,
			Source:      domain.SourceManual,
			OwnerUserID: cfg.User.ID,
			Title:       noteTitle,
			Content:     content,
			Tags:        noteTags,
			MIME:        mime,
			SourceMeta:  sourceMeta,
		})
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
		defer a.Close()
		if noteLimit <= 0 {
			noteLimit = 20
		}
		items, _, err := a.Items.List(cmd.Context(), port.ItemFilter{
			Scopes:      []domain.Scope{domain.ScopeUser},
			OwnerUserID: cfg.User.ID,
			Kinds:       []domain.Kind{domain.KindNote},
			Tags:        noteTagsFilter,
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
			fmt.Println(formatListItem(it))
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
		defer a.Close()
		item, err := a.Items.Get(cmd.Context(), args[0])
		if err != nil {
			return err
		}
		content := item.Content
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
		defer a.Close()
		if err := a.Items.Delete(cmd.Context(), args[0]); err != nil {
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
	userNoteAddCmd.Flags().StringVar(&noteFilePath, "file", "", "import content from a file (text only)")
	userNoteListCmd.Flags().StringSliceVar(&noteTagsFilter, "tag", nil, "filter by tag (OR semantics; comma-separated or repeat)")
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

// listPreviewLen is the maximum rune count of content shown as a fallback
// title in list output when an item has no title.
const listPreviewLen = 50

// formatListItem renders one row of `user note list`. When the item has a
// non-empty title, the title is shown verbatim. When the title is empty
// (common when `add` was called without --title), a preview of the inline
// content is shown instead so the user sees something useful. When content
// is also empty (externalized to FileStore), an "(externalized)" placeholder
// is shown — the full content can always be retrieved with `get <id>`.
func formatListItem(item domain.ContextItem) string {
	label := item.Title
	if label == "" {
		switch {
		case item.Content != "":
			label = previewRunes(item.Content, listPreviewLen)
		case item.ContentURI != "":
			label = "(externalized)"
		default:
			label = "(no content)"
		}
	}
	tags := strings.Join(item.Tags, ",")
	return fmt.Sprintf("%s  %s  [%s]", item.ID, label, tags)
}

// previewRunes returns the first n runes of s, appending an ellipsis if s
// was truncated.
func previewRunes(s string, n int) string {
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n]) + "…"
}

// maxFileBytes is the file import size cap. Enforced via os.Stat before
// os.ReadFile so a rejected file never allocates a buffer. 10 MB is a
// guardrail against accidentally loading huge files, not a security boundary.
const maxFileBytes int64 = 10 * 1024 * 1024

// mimeForTextFile maps a small set of text file extensions to MIME types.
// Unknown extensions default to text/plain — binary support is out of scope.
// Case-insensitive via strings.ToLower so weekly.MD is still markdown.
// Adding new text types later (.org, .rst) is a one-liner here.
func mimeForTextFile(path string) string {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".md", ".markdown":
		return "text/markdown"
	default:
		return "text/plain"
	}
}

// deriveDefaultTitle extracts a human-friendly title from a file path by
// taking the basename and stripping the last extension. Used when the user
// runs `--file weekly.md` without `--title`. Only the last extension is
// stripped (archive.tar.gz → "archive.tar") to match user intuition.
// A leading-dot file (.bashrc) keeps its full basename (dot at index 0
// is not stripped).
func deriveDefaultTitle(path string) string {
	base := filepath.Base(path)
	if dot := strings.LastIndex(base, "."); dot > 0 {
		base = base[:dot]
	}
	return base
}

// checkFileSize is a pure function so tests can sweep synthetic sizes
// (0, at-cap, cap+1) without writing real fixtures to disk.
func checkFileSize(size int64) error {
	if size > maxFileBytes {
		return fmt.Errorf("file too large: %d bytes (max %d)", size, maxFileBytes)
	}
	return nil
}

// validateFileImport runs the file-level validation rules (Rules 2-4 from
// the spec): file must exist, be a regular file, and be within the size cap.
// os.Stat runs before any os.ReadFile so oversized files are rejected
// without allocating a buffer. Rule 0 (empty path) and Rule 1 (mutual
// exclusion with positional args) are handled in RunE before this helper.
func validateFileImport(path string) error {
	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("stat file: %w", err)
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("not a regular file: %s", path)
	}
	if err := checkFileSize(info.Size()); err != nil {
		return err
	}
	return nil
}
