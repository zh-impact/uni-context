package service

import (
	"context"
	"fmt"
	"strings"
	"unicode"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

type IngestService struct {
	repo port.ContextRepo
	fs   port.FileStore
}

func NewIngestService(repo port.ContextRepo, fs port.FileStore) *IngestService {
	return &IngestService{repo: repo, fs: fs}
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

	if len(in.Content) > domain.ContentInlineLimit {
		uri, hash, err := s.fs.Put([]byte(in.Content), "text/plain")
		if err != nil {
			return "", fmt.Errorf("externalize content: %w", err)
		}
		item.ContentURI = uri
		item.ContentHash = hash
		item.ContentMIME = "text/plain"
		item.Content = ""
	} else {
		item.Content = in.Content
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
		if !inWord {
			n++
			inWord = true
		}
	}
	return n
}
