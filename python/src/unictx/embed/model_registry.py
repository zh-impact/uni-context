"""ModelRegistry Protocol + ModelSpec + ModelDescriptor.

Ports Go's internal/port/modelregistry.go. Two dataclasses because Go
has two: ModelSpec is the *input* to ModelRegistry.register;
ModelDescriptor is the *output* of list/get/get_active (the full
projection of an embedding_model row, including fields the caller
never sets like is_default and status).

The task brief lists only ModelSpec, but ModelDescriptor is required
for the Protocol's return types to type-check. Added as a faithful
port — documented as a minor deviation from the brief's dataclass list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class ModelSpec:
    """Input to ModelRegistry.register. Mirrors Go's port.ModelSpec.

    Slug is the user-facing identifier; provider/base_url/api_key come
    from the row's config JSON column; dimension determines the
    vec_<slug>_<dim> table name.
    """

    slug: str = ""
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    dimension: int = 0


@dataclass(slots=True)
class ModelDescriptor:
    """Full projection of an embedding_model row. Mirrors Go's port.ModelDescriptor.

    Returned by list/get/get_active. Includes fields the caller never
    sets on insert (is_default, status, vec_table) but that the
    registry owns internally.
    """

    slug: str = ""
    name: str = ""
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    dimension: int = 0
    vec_table: str = ""
    is_default: bool = False
    status: str = ""  # "active" | "disabled"


@runtime_checkable
class ModelRegistry(Protocol):
    """Owns the embedding_model table. Mirrors Go's port.ModelRegistry.

    Methods are concurrency-safe via the underlying storage connection
    pool; callers do not need external locking.

    Error semantics:
      - get/get_active/remove/set_default/update_config raise
        ModelNotFound when slug is absent.
      - register raises ModelConflict on UNIQUE violation (slug exists).
      - remove raises a domain error if slug is_default=1 (caller must
        switch first) or vec_table is shared with another slug.
    """

    def list(self) -> list[ModelDescriptor]:
        """All registered models, ordered by created_at ASC."""
        ...

    def get_active(self) -> ModelDescriptor:
        """The row with is_default=True. Raise ModelNotFound if no default."""
        ...

    def get(self, slug: str) -> ModelDescriptor:
        """The row for slug. Raise ModelNotFound if not registered."""
        ...

    def register(self, spec: ModelSpec) -> None:
        """Insert a new model row and create its vec_<slug>_<dim> virtual table.

        Strict INSERT: raise ModelConflict if slug already exists.
        Callers needing upsert behavior must explicitly check get first
        and call update_config.
        """
        ...

    def update_config(self, slug: str, base_url: str, api_key: str, provider: str) -> None:
        """Overwrite provider + config JSON for an existing slug.

        Raise ModelNotFound if slug does not exist.
        """
        ...

    def set_default(self, slug: str) -> None:
        """Flip is_default atomically: slug=True, all others=False.

        Idempotent if slug is already default. Raise ModelNotFound if
        slug does not exist.
        """
        ...

    def remove(self, slug: str) -> None:
        """Drop the model's vec table and delete its embedding_model row.

        context_embedding rows referencing this slug are deleted
        explicitly inside the implementation's transaction (the FK is
        RESTRICT, so the explicit DELETE is mandatory).

        Raises ModelNotFound if slug does not exist. Raises a domain
        error if slug is_default=True (caller must switch first) or if
        vec_table is referenced by another slug (shared-table protection).
        """
        ...
