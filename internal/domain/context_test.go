package domain

import (
    "strings"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
)

func TestNewContextItem_ValidCombinations(t *testing.T) {
    cases := []struct {
        name   string
        scope  Scope
        kind   Kind
        source Source
        owner  string
        project string
    }{
        {"user note manual", ScopeUser, KindNote, SourceManual, "user-1", ""},
        {"project doc import", ScopeProject, KindDoc, SourceImport, "user-1", "proj-1"},
        {"global link import", ScopeGlobal, KindLink, SourceImport, "", ""},
        {"project conversation agent", ScopeProject, KindConversationMsg, SourceAgent, "", "proj-1"},
        {"project memory sync", ScopeProject, KindMemory, SourceSync, "", "proj-1"},
    }
    for _, tc := range cases {
        t.Run(tc.name, func(t *testing.T) {
            item, err := NewContextItem(tc.scope, tc.kind, tc.source, NewItemParams{
                OwnerUserID: tc.owner, ProjectID: tc.project,
            })
            require.NoError(t, err)
            assert.NotEmpty(t, item.ID)
            assert.Equal(t, tc.scope, item.Scope)
        })
    }
}

func TestNewContextItem_InvalidCombinations(t *testing.T) {
    cases := []struct {
        name   string
        scope  Scope
        kind   Kind
        source Source
        owner  string
        project string
        wantErrSub string
    }{
        {"global with owner", ScopeGlobal, KindNote, SourceManual, "u", "", "global must not have owner"},
        {"global with project", ScopeGlobal, KindNote, SourceManual, "", "p", "global must not have project"},
        {"user without owner", ScopeUser, KindNote, SourceManual, "", "", "user scope requires owner"},
        {"project without project", ScopeProject, KindDoc, SourceManual, "u", "", "project scope requires project_id"},
        {"memory without agent source", ScopeProject, KindMemory, SourceManual, "u", "p", "memory kind requires source=agent or sync"},
        {"memory with wrong scope", ScopeUser, KindMemory, SourceAgent, "u", "", "memory kind requires project scope"},
    }
    for _, tc := range cases {
        t.Run(tc.name, func(t *testing.T) {
            _, err := NewContextItem(tc.scope, tc.kind, tc.source, NewItemParams{
                OwnerUserID: tc.owner, ProjectID: tc.project,
            })
            require.Error(t, err)
            assert.True(t, strings.Contains(err.Error(), tc.wantErrSub),
                "err=%q want substring %q", err.Error(), tc.wantErrSub)
        })
    }
}

func TestNewContextItem_AssignsUUIDv7(t *testing.T) {
    item1, _ := NewContextItem(ScopeUser, KindNote, SourceManual, NewItemParams{OwnerUserID: "u"})
    item2, _ := NewContextItem(ScopeUser, KindNote, SourceManual, NewItemParams{OwnerUserID: "u"})
    assert.NotEqual(t, item1.ID, item2.ID)
    // UUIDv7 has 48-bit timestamp prefix; first char should match for nearby IDs
    assert.Equal(t, item1.ID[:1], item2.ID[:1])
}
