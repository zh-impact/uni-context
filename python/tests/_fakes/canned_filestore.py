"""CannedFileStore — strict-lookup FileStore for service-layer tests.

Ports Go's internal/service/reindex_fts_test.go:cannedFileStore. Differences:

  - get raises items/errors.py:ExternalizedContentMissing on unknown URI
    (Go returns a generic error). Using the production error class matches
    the FileStore Protocol's documented contract and lets tests catch
    UnictxError the same way CLI does.
  - delete APPENDS to `deleted_uris` and removes the URI from the dict
    if present (Go's cannedFileStore.Delete was a no-op). Recording
    deletes is what Phase 5 rollback tests need.
  - put stores content in-dict keyed by sha256 hash so subsequent get
    calls can find it. Not idempotent-by-hash like the production impl
    — tests that need idempotency should mock differently or use the
    real storage/ impl (Phase 2). This matches the brief: "stub, not
    real impl."
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from unictx.items.errors import ExternalizedContentMissing


@dataclass(slots=True)
class CannedFileStore:
    """Dict-backed FileStore. Records delete calls for rollback tests.

    Attributes:
      data: uri -> bytes. Tests seed this via the constructor or by
        mutating after construction.
      deleted_uris: list of URIs passed to delete(), in call order.
        Phase 5 rollback tests assert this list to verify cleanup.
      put_calls: list of (mime, content_len) tuples, in call order.
        Useful when a test needs to assert put was invoked at all.
    """

    data: dict[str, bytes] = field(default_factory=dict)
    deleted_uris: list[str] = field(default_factory=list)
    put_calls: list[tuple[str, int]] = field(default_factory=list)

    def put(self, content: bytes, mime: str) -> tuple[str, str]:
        # Brief says this is a stub, not a real impl — derive a stable
        # URI from sha256 so get() round-trips. Production impl (Phase 2)
        # uses file://<relative-path>; we use sha256://<hash> to make
        # test assertions readable and avoid filesystem coupling.
        h = hashlib.sha256(content).hexdigest()
        uri = f"sha256://{h}"
        self.data[uri] = content
        self.put_calls.append((mime, len(content)))
        return uri, h

    def get(self, uri: str) -> bytes:
        try:
            return self.data[uri]
        except KeyError as exc:
            raise ExternalizedContentMissing(uri) from exc

    def delete(self, uri: str) -> None:
        # Record first, then mutate — tests want the call log even if
        # the URI was never inserted (defensive delete path).
        self.deleted_uris.append(uri)
        self.data.pop(uri, None)
