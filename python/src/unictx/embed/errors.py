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


class CorruptConfigError(UnictxError):
    """Raised by ModelRegistry scan helper when ``config`` JSON is unparseable.

    Ports Go's ``ErrCorruptConfig`` sentinel. The descriptor's identity
    fields (slug/name/provider/dimension/vec_table/is_default/status)
    scan cleanly; only the embedded ``base_url``/``api_key`` are
    unrecoverable. Callers needing only the identity — e.g.
    ``set_default`` — can ignore this error; callers that need the
    config must heal the row first.

    The descriptor is attached as :attr:`descriptor` so callers can
    inspect what they did recover.
    """

    def __init__(self, message: str, *, descriptor: object) -> None:
        super().__init__(message)
        self.descriptor = descriptor


class SchemaMetaNotFound(UnictxError):
    """Raised by SchemaMetaImpl.version when no ``schema_version`` row exists.

    Mirrors Go's wrapped error from ``Version``: the ``schema_meta``
    table is empty or missing the ``schema_version`` key. Should be
    unreachable on a normally-bootstrapped DB (the migration runner
    seeds the key on first run), but surfaces cleanly for misconfigured
    read-only connections or hand-edited DBs.
    """

    def __init__(self) -> None:
        super().__init__("schema_meta row missing: key='schema_version'")


class InvalidSlugError(ValueError):
    """Raised when a model slug contains characters unsafe for SQL identifier use.

    Slugs flow into ``_vec_table_name`` and are interpolated into raw
    SQL (``CREATE VIRTUAL TABLE``, ``DROP TABLE``, vec0 DML) — see
    :func:`unictx.storage.model_registry_impl._vec_table_name`. A slug
    containing shell-meta, semicolons, parens, or quotes could break
    out of the SQL identifier and inject statements.

    Inherits from :class:`ValueError` (not :class:`UnictxError`) because
    this is **input validation** — "the caller handed us a bad value" —
    not a domain-level failure like "model not found" or "model
    conflict". Callers can catch :class:`ValueError` generically or
    :class:`InvalidSlugError` specifically.
    """

    def __init__(self, slug: str):
        super().__init__(f"invalid model slug {slug!r}: must match [a-zA-Z0-9_-]+")
        self.slug = slug
