"""items module — domain-specific errors.

All inherit from unictx.errors.UnictxError so CLI can catch them
collectively via `except UnictxError`.
"""

from unictx.errors import UnictxError


class ItemNotFound(UnictxError):
    """Raised by ContextRepo.get when no row matches the id."""

    def __init__(self, item_id: str):
        super().__init__(f"item not found: {item_id}")
        self.item_id = item_id


class ExternalizedContentMissing(UnictxError):
    """Raised when item.content_uri is set but FileStore has no blob.

    Indicates filestore/repo divergence — either the blob was deleted
    out-of-band, or the URI is corrupt.
    """

    def __init__(self, uri: str):
        super().__init__(f"externalized content missing: {uri}")
        self.uri = uri


class ItemValidationError(UnictxError):
    """Raised by NewContextItem when params violate a combination rule.

    Mirrors Go's validateCombination failures. Use this for illegal
    scope/kind/source/content combinations.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
