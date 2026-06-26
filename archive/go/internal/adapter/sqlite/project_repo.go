package sqlite

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"uni-context/internal/domain"
)

type ProjectRepo struct {
	db *sql.DB
}

func NewProjectRepo(db *sql.DB) *ProjectRepo {
	return &ProjectRepo{db: db}
}

func (r *ProjectRepo) Create(ctx context.Context, p domain.Project) error {
	_, err := r.db.ExecContext(ctx, `
        INSERT INTO project (id, name, path, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)`,
		p.ID, p.Name, nullable(p.Path), nullable(p.Description),
		p.CreatedAt.Unix(), p.UpdatedAt.Unix(),
	)
	if err != nil {
		return fmt.Errorf("insert project: %w", err)
	}
	return nil
}

func (r *ProjectRepo) GetByName(ctx context.Context, name string) (domain.Project, error) {
	var p domain.Project
	var path, desc sql.NullString
	var created, updated int64
	err := r.db.QueryRowContext(ctx,
		`SELECT id, name, path, description, created_at, updated_at FROM project WHERE name=?`,
		name,
	).Scan(&p.ID, &p.Name, &path, &desc, &created, &updated)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.Project{}, fmt.Errorf("%w: project %s", domain.ErrNotFound, name)
	}
	if err != nil {
		return domain.Project{}, fmt.Errorf("get project: %w", err)
	}
	p.Path = path.String
	p.Description = desc.String
	p.CreatedAt = timeFromUnix(created)
	p.UpdatedAt = timeFromUnix(updated)
	return p, nil
}

func (r *ProjectRepo) List(ctx context.Context) ([]domain.Project, error) {
	rows, err := r.db.QueryContext(ctx,
		`SELECT id, name, path, description, created_at, updated_at FROM project ORDER BY name`,
	)
	if err != nil {
		return nil, fmt.Errorf("list projects: %w", err)
	}
	defer rows.Close()
	var out []domain.Project
	for rows.Next() {
		var p domain.Project
		var path, desc sql.NullString
		var created, updated int64
		if err := rows.Scan(&p.ID, &p.Name, &path, &desc, &created, &updated); err != nil {
			return nil, err
		}
		p.Path = path.String
		p.Description = desc.String
		p.CreatedAt = timeFromUnix(created)
		p.UpdatedAt = timeFromUnix(updated)
		out = append(out, p)
	}
	return out, rows.Err()
}

func (r *ProjectRepo) Delete(ctx context.Context, id string) error {
	res, err := r.db.ExecContext(ctx, `DELETE FROM project WHERE id=?`, id)
	if err != nil {
		return fmt.Errorf("delete project: %w", err)
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return fmt.Errorf("%w: project %s", domain.ErrNotFound, id)
	}
	return nil
}
