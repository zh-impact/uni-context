"""embed module — embedding-related errors.

All inherit from unictx.errors.UnictxError so CLI can catch them
collectively via `except UnictxError`. Attribute pattern mirrors
items/errors.py: each error carries the identifiers a CLI error
renderer would want (slug, item_id, reason).
"""

from unictx.errors import UnictxError


class ModelNotFound(UnictxError):
    """Raised by ModelRegistry.get/get_active/remove/set_default/update_config.

    Also raised by ModelRegistry.get_active when no row has
    is_default=True — the caller asked for "the active model" and
    there isn't one.
    """

    def __init__(self, slug: str):
        super().__init__(f"embedding model not found: {slug}")
        self.slug = slug


class ModelConflict(UnictxError):
    """Raised by ModelRegistry.register on UNIQUE violation.

    The slug is already registered. Caller must use update_config to
    mutate an existing row, or pick a different slug.
    """

    def __init__(self, slug: str):
        super().__init__(f"embedding model already exists: {slug}")
        self.slug = slug


class EmbeddingFailed(UnictxError):
    """Raised by Embedder.embed on backend failure.

    Wraps the underlying error (HTTP 5xx, network failure, malformed
    response). Carries the model slug so the CLI can identify which
    embedder failed in a multi-model deployment.
    """

    def __init__(self, slug: str, reason: str):
        super().__init__(f"embedding failed for model {slug!r}: {reason}")
        self.slug = slug
        self.reason = reason


class StatusNotFound(UnictxError):
    """Raised by EmbeddingRepo.get_status when no row matches.

    The (item_id, model_slug) pair has no status row — either the item
    was never embedded, or it was embedded under a different model.
    """

    def __init__(self, item_id: str, model_slug: str):
        super().__init__(f"embedding status not found: item={item_id} model={model_slug}")
        self.item_id = item_id
        self.model_slug = model_slug
