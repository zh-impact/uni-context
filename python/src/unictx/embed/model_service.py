"""ModelService — application-layer boundary for embedding-model lifecycle.

Behavior-port of Go's ``internal/service/model.go``. Thin pass-through
over ``ModelRegistry`` + ``EmbeddingRepo`` — the value is the boundary,
not added logic. Routing ``embed model add/list/remove``, ``embed switch``,
and ``embed status`` through this service means the CLI has no direct
dependency on those ports, so the registry implementation can change
(e.g. switch to a different metadata store) without touching the
inbound layer.

Plan §Python Conventions adaptations:
  - ctx dropped (Python is sync).
  - Go's tuple returns ``(T, error)`` → Python returns ``T`` or raises.
  - ``NewModelService`` → ``__init__``; the "embedder not enabled"
    guard lives in the CLI — when Plan 1 is active the service is not
    constructed at all (App.Models stays None).
"""

from __future__ import annotations

from unictx.embed.embedding_repo import EmbeddingStatus
from unictx.embed.model_registry import ModelDescriptor, ModelRegistry, ModelSpec

__all__ = ["ModelService"]


class ModelService:
    """Application-layer boundary for embedding-model lifecycle.

    Constructed with a ``ModelRegistry`` (lifecycle) + ``EmbeddingRepo``
    (per-item status rows). Both are required; the CLI gates construction
    on ``embedder.enabled``.
    """

    def __init__(
        self,
        registry: ModelRegistry,
        emb_repo,
    ) -> None:
        self._registry = registry
        self._emb_repo = emb_repo

    def add_model(self, spec: ModelSpec) -> None:
        """Register a new embedding model and create its vec table.

        Forwards verbatim to ModelRegistry.register — slug conflicts,
        dimension validation, and provider checks are the registry's
        responsibility. Raises ``ModelConflict`` on UNIQUE violation.
        """
        self._registry.register(spec)

    def list_models(self) -> list[ModelDescriptor]:
        """All registered models, ordered by created_at ASC."""
        return self._registry.list()

    def remove_model(self, slug: str) -> None:
        """Drop the model's vec table + delete its embedding_model row.

        Refuses the default model and shared tables — those rules live
        in the registry and surface as errors here.
        """
        self._registry.remove(slug)

    def switch_model(self, slug: str) -> None:
        """Flip is_default atomically to the named slug.

        The post-switch "run reembed" reminder stays in the CLI — it's
        a UI concern, not a service-layer invariant.
        """
        self._registry.set_default(slug)

    def item_embedding_status(self, item_id: str) -> list[EmbeddingStatus]:
        """All context_embedding rows for one item, ordered by model_slug ASC.

        Empty list (not None) when no rows exist. Used by
        ``embed status <id>`` to show per-model migration state.
        """
        return self._emb_repo.list_for_item(item_id)
