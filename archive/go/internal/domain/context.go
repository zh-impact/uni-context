package domain

import (
    "fmt"
    "time"

    "github.com/google/uuid"
)

// Scope of a ContextItem.
type Scope string

const (
    ScopeUser    Scope = "user"
    ScopeProject Scope = "project"
    ScopeGlobal  Scope = "global"
)

// Kind of content.
type Kind string

const (
    KindNote            Kind = "note"
    KindExcerpt         Kind = "excerpt"
    KindLink            Kind = "link"
    KindDoc             Kind = "doc"
    KindConversationMsg Kind = "conversation_msg"
    KindMemory          Kind = "memory"
    KindFile            Kind = "file"
)

// Source of a ContextItem.
type Source string

const (
    SourceManual  Source = "manual"
    SourceAgent   Source = "agent"
    SourceSync    Source = "sync"
    SourceImport  Source = "import"
    SourceWebhook Source = "webhook"
)

// Visibility of a ContextItem.
type Visibility string

const (
    VisibilityPrivate Visibility = "private"
    VisibilityProject Visibility = "project"
    VisibilityPublic  Visibility = "public"
)

// ContextItem is the unified entity for all knowledge in the system.
type ContextItem struct {
    ID             string
    Scope          Scope
    Kind           Kind
    Source         Source
    OwnerUserID    string
    ProjectID      string
    AgentID        string
    ConversationID string
    ParentID       string

    Title    string
    Summary  string
    Content  string // inline, <= 4KB
    ContentURI string
    ContentMIME string
    ContentHash string
    Language string

    Tags       []string
    SourceMeta map[string]any
    Visibility Visibility
    Confidence float64

    WordCount   int
    AnyEmbedding int // 0 or 1; always 0 in Plan 1
    CreatedAt   time.Time
    UpdatedAt   time.Time
    Version     int
}

// NewItemParams holds optional fields for NewContextItem.
type NewItemParams struct {
    OwnerUserID string
    ProjectID   string
    AgentID     string
}

// ContentInlineLimit is the max byte length stored inline in Content.
const ContentInlineLimit = 4 * 1024

// NewContextItem constructs an item with scope/kind/source invariants enforced.
func NewContextItem(scope Scope, kind Kind, source Source, params NewItemParams) (ContextItem, error) {
    if err := validateCombination(scope, kind, source, params); err != nil {
        return ContextItem{}, fmt.Errorf("%w: %s", ErrValidation, err.Error())
    }
    now := time.Now().UTC()
    id, err := uuid.NewV7()
    if err != nil {
        return ContextItem{}, fmt.Errorf("generate id: %w", err)
    }
    return ContextItem{
        ID:          id.String(),
        Scope:       scope,
        Kind:        kind,
        Source:      source,
        OwnerUserID: params.OwnerUserID,
        ProjectID:   params.ProjectID,
        AgentID:     params.AgentID,
        Visibility:  VisibilityPrivate,
        Confidence:  1.0,
        Tags:        []string{},
        SourceMeta:  map[string]any{},
        CreatedAt:   now,
        UpdatedAt:   now,
        Version:     1,
    }, nil
}

func validateCombination(scope Scope, kind Kind, source Source, p NewItemParams) error {
    switch scope {
    case ScopeGlobal:
        if p.OwnerUserID != "" {
            return fmt.Errorf("global must not have owner")
        }
        if p.ProjectID != "" {
            return fmt.Errorf("global must not have project")
        }
    case ScopeUser:
        if p.OwnerUserID == "" {
            return fmt.Errorf("user scope requires owner")
        }
        if p.ProjectID != "" {
            return fmt.Errorf("user scope must not have project (use project scope)")
        }
    case ScopeProject:
        if p.ProjectID == "" {
            return fmt.Errorf("project scope requires project_id")
        }
    default:
        return fmt.Errorf("unknown scope %q", scope)
    }

    if kind == KindMemory {
        if scope != ScopeProject {
            return fmt.Errorf("memory kind requires project scope")
        }
        if source != SourceAgent && source != SourceSync {
            return fmt.Errorf("memory kind requires source=agent or sync")
        }
    }
    if kind == KindConversationMsg {
        if scope != ScopeProject {
            return fmt.Errorf("conversation_msg kind requires project scope")
        }
        if source != SourceAgent && source != SourceSync {
            return fmt.Errorf("conversation_msg kind requires source=agent or sync")
        }
    }
    return nil
}
