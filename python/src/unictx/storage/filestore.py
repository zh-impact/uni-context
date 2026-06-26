"""FileStore Protocol — large-content blob storage.

Ports Go's internal/port/filestore.go. Lives in storage/ (not items/)
because storage/ owns the impl too — see Plan §Module Structure.

Error semantics:
  - put does not raise on existing content; returns the existing URI.
  - get raises items/errors.py:ExternalizedContentMissing when the URI
    has no blob. This indicates filestore/repo divergence — either the
    blob was deleted out-of-band, or the URI is corrupt. Re-using the
    items-defined error type (no new error class in this module) keeps
    the CLI's catch-UnictxError path simple and matches Go's behavior
    of wrapping a single domain error.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class FileStore(Protocol):
    """Holds large content blobs (>4KB) on disk, addressed by sha256 hash.

    Mirrors Go's port.FileStore. Implementations live in storage/ (Phase 2).
    """

    def put(self, content: bytes, mime: str) -> tuple[str, str]:
        """Write content and return (content_uri, sha256_hash).

        content_uri is "file://<relative-path>". If content already
        exists (matching hash), returns existing URI — idempotent.
        """
        ...

    def get(self, uri: str) -> bytes:
        """Retrieve content by uri. Raise ExternalizedContentMissing if absent.

        ExternalizedContentMissing is imported from items/errors.py —
        see module docstring for the rationale (no new error class here).
        """
        ...

    def delete(self, uri: str) -> None:
        """Decrement refcount; file removed only when refcount hits 0.

        No-op (idempotent) if uri absent.
        """
        ...
