package service

import (
	"context"
	"fmt"
	"os"
	"strings"
	"unicode"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

type IngestService struct {
	repo  port.ContextRepo
	fs    port.FileStore
	embed *EmbedService // nil = embedding disabled (Plan 1 compat)
}

func NewIngestService(repo port.ContextRepo, fs port.FileStore) *IngestService {
	return &IngestService{repo: repo, fs: fs}
}

// NewIngestServiceWithEmbedder wires an optional EmbedService. If embed
// is nil, behavior is identical to NewIngestService (Plan 1: no vector
// writes). When non-nil, Create embeds synchronously after a successful
// repo.Create; embed failure is non-fatal (warned to stderr, item is
// still returned and FTS-searchable).
func NewIngestServiceWithEmbedder(repo port.ContextRepo, fs port.FileStore, embed *EmbedService) *IngestService {
	return &IngestService{repo: repo, fs: fs, embed: embed}
}

// Input is the user-facing write request.
type Input struct {
	Scope       domain.Scope
	Kind        domain.Kind
	Source      domain.Source
	OwnerUserID string
	ProjectID   string
	AgentID     string

	Title   string
	Summary string
	Content string
	Tags    []string

	SourceMeta map[string]any

	// MIME of the content. Empty means "treat as text/plain" (preserves
	// existing behavior for inline/stdin notes — no caller update required).
	// Set by the CLI when importing a .md file so FileStore's .meta and
	// item.ContentMIME both carry the right MIME. For inline-sized file
	// imports (< ContentInlineLimit), MIME is still written to item.ContentMIME
	// so downstream renderers can tell a .md import from a .txt import even
	// when the bytes didn't go through FileStore.
	MIME string
}

func (s *IngestService) Create(ctx context.Context, in Input) (string, error) {
	item, err := domain.NewContextItem(in.Scope, in.Kind, in.Source, domain.NewItemParams{
		OwnerUserID: in.OwnerUserID,
		ProjectID:   in.ProjectID,
		AgentID:     in.AgentID,
	})
	if err != nil {
		return "", err
	}

	item.Title = strings.TrimSpace(in.Title)
	item.Summary = in.Summary
	item.Tags = in.Tags
	if item.Tags == nil {
		item.Tags = []string{}
	}
	item.SourceMeta = in.SourceMeta
	if item.SourceMeta == nil {
		item.SourceMeta = map[string]any{}
	}
	item.WordCount = countWords(in.Content)

	mime := in.MIME
	if mime == "" {
		mime = "text/plain"
	}
	if len(in.Content) > domain.ContentInlineLimit {
		uri, hash, err := s.fs.Put([]byte(in.Content), mime)
		if err != nil {
			return "", fmt.Errorf("externalize content: %w", err)
		}
		item.ContentURI = uri
		item.ContentHash = hash
		item.ContentMIME = mime
		item.Content = ""
	} else {
		item.Content = in.Content
		if in.MIME != "" {
			item.ContentMIME = in.MIME
		}
	}

	if err := s.repo.Create(ctx, item); err != nil {
		// Roll back the filestore entry we just bumped. Without this,
		// a failed repo.Create leaves an orphaned refcount=1 blob that
		// nothing references. fs.Delete decrements refcount; when it
		// hits 0 the file is removed. Only relevant when we externalized
		// (item.ContentURI != "").
		if item.ContentURI != "" {
			_ = s.fs.Delete(item.ContentURI)
		}
		return "", fmt.Errorf("persist item: %w", err)
	}

	// Externalized-content fix: the AFTER INSERT trigger on context_item
	// wrote an FTS row reading new.content, which is "" for externalized
	// items (bytes live in FileStore). Without this rewrite the item is
	// silently unsearchable via FTS — `search "keyword"` returns 0 hits
	// even when the keyword exists in the file. ReindexFTS rewrites the
	// FTS row with the hydrated content. We still have in.Content in
	// memory here; no FileStore round-trip needed.
	//
	// Non-fatal: if ReindexFTS fails, the item is already saved and the
	// `unictx reindex-fts` CLI command can fix it later. The alternative
	// (failing the whole Create) would punish the user for a search-only
	// index bug, which doesn't match how we treat embed failures.
	if item.ContentURI != "" {
		if err := s.repo.ReindexFTS(ctx, item.ID, item.Title, item.Summary, in.Content); err != nil {
			fmt.Fprintf(os.Stderr, "warn: reindex fts for %s: %v\n", item.ID, err)
		}
	}

	// Synchronous embed after the item is durably persisted. Embedding
	// failure is non-fatal — the item is already saved and FTS-searchable,
	// and the async worker (Plan 2b) will retry on its next iteration.
	// any_embedding stays 0 until the worker flips it; SearchService
	// treats 0 as "not vector-searchable" (correct).
	//
	// For externalized items, item.Content is "" here and EmbedService
	// hydrates from FileStore via item.ContentURI.
	if s.embed != nil {
		if err := s.embed.Embed(ctx, item.ID, item.Title, item.Content); err != nil {
			fmt.Fprintf(os.Stderr, "warn: embed failed for %s: %v\n", item.ID, err)
		}
	}
	return item.ID, nil
}

func countWords(s string) int {
	n := 0
	inWord := false
	for _, r := range s {
		if unicode.IsSpace(r) {
			inWord = false
			continue
		}
		if isCJK(r) {
			inWord = false
			n++
			continue
		}
		if !inWord {
			n++
			inWord = true
		}
	}
	return n
}

// isCJK reports whether r is a CJK ideograph or related script character
// that should be counted as one word each (no space delimiters).
func isCJK(r rune) bool {
	switch {
	case 0x4E00 <= r && r <= 0x9FFF: // CJK Unified Ideographs
		return true
	case 0x3400 <= r && r <= 0x4DBF: // CJK Extension A
		return true
	case 0x3040 <= r && r <= 0x30FF: // Hiragana + Katakana
		return true
	case 0xAC00 <= r && r <= 0xD7AF: // Hangul Syllables
		return true
	case 0xF900 <= r && r <= 0xFAFF: // CJK Compatibility Ideographs
		return true
	case 0x31C0 <= r && r <= 0x31EF: // CJK Strokes
		return true
	}
	return false
}
