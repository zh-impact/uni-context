package domain

import "errors"

var (
    ErrNotFound   = errors.New("not found")
    ErrValidation = errors.New("validation")
    ErrConflict   = errors.New("conflict")
)
