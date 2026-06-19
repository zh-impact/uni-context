package domain

import (
	"fmt"
	"time"

	"github.com/google/uuid"
)

type Project struct {
	ID          string
	Name        string
	Path        string
	Description string
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

func NewProject(name, path, description string) (Project, error) {
	if name == "" {
		return Project{}, fmt.Errorf("%w: project name required", ErrValidation)
	}
	id, err := uuid.NewV7()
	if err != nil {
		return Project{}, fmt.Errorf("generate id: %w", err)
	}
	now := time.Now().UTC()
	return Project{
		ID: id.String(), Name: name, Path: path, Description: description,
		CreatedAt: now, UpdatedAt: now,
	}, nil
}
