package domain

import (
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
)

func TestNewProject_AssignsIDAndTimestamps(t *testing.T) {
    p, err := NewProject("my-app", "/path/to/app", "desc")
    require.NoError(t, err)
    assert.NotEmpty(t, p.ID)
    assert.Equal(t, "my-app", p.Name)
    assert.False(t, p.CreatedAt.IsZero())
}

func TestNewProject_RejectsEmptyName(t *testing.T) {
    _, err := NewProject("", "/path", "")
    require.Error(t, err)
}
