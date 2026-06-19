package sqlite

import (
	"context"
	"database/sql"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"uni-context/internal/domain"
	"uni-context/internal/port"
)

func setupProjectRepo(t *testing.T) port.ProjectRepo {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	require.NoError(t, err)
	require.NoError(t, Migrate(db))
	t.Cleanup(func() { db.Close() })
	return NewProjectRepo(db)
}

func TestProjectRepo_CRUD(t *testing.T) {
	repo := setupProjectRepo(t)
	ctx := context.Background()

	p, err := domain.NewProject("my-app", "/path/to/app", "test project")
	require.NoError(t, err)
	require.NoError(t, repo.Create(ctx, p))

	got, err := repo.GetByName(ctx, "my-app")
	require.NoError(t, err)
	assert.Equal(t, p.ID, got.ID)

	list, err := repo.List(ctx)
	require.NoError(t, err)
	assert.Len(t, list, 1)

	require.NoError(t, repo.Delete(ctx, p.ID))
	_, err = repo.GetByName(ctx, "my-app")
	assert.ErrorIs(t, err, domain.ErrNotFound)
}
