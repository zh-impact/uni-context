"""FileStore Protocol + impl — large-content blob storage.

Protocol ports Go's internal/port/filestore.go. Impl ports Go's
internal/adapter/fsstore/store.go. Both live in storage/ (not items/)
because storage/ owns the impl too — see Plan §Module Structure.

Error semantics:
  - put does not raise on existing content; returns the existing URI.
  - get raises items/errors.py:ExternalizedContentMissing when the URI
    has no blob. This indicates filestore/repo divergence — either the
    blob was deleted out-of-band, or the URI is corrupt. Re-using the
    items-defined error type (no new error class in this module) keeps
    the CLI's catch-UnictxError path simple and matches Go's behavior
    of wrapping a single domain error.

On-disk layout (identical to Go):
  <root>/<hex[:2]>/<hex>        — content bytes
  <root>/<hex[:2]>/<hex>.meta   — JSON {"refcount", "mime", "size"}

Thread-safety: a threading.Lock guards refcount mutations and the
put-once-write-meta-once critical section, mirroring Go's sync.Mutex.
Stdlib only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from unictx.items.errors import ExternalizedContentMissing


@runtime_checkable
class FileStore(Protocol):
    """Holds large content blobs (>4KB) on disk, addressed by sha256 hash.

    Mirrors Go's port.FileStore. Implementations live in storage/ (Phase 2).
    """

    def put(self, content: bytes, mime: str) -> tuple[str, str]:
        """Write content and return (content_uri, sha256_hash).

        content_uri is "file://<sha256-hex>". If content already
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


# ---------------------------------------------------------------------------
# FileStoreImpl — ports Go's internal/adapter/fsstore/store.go
# ---------------------------------------------------------------------------

_HASH_HEX_LEN = 64  # sha256 hex length
_URI_SCHEME = "file://"


class FileStoreImpl:
    """sha256-addressed, refcounted FileStore backed by a directory tree.

    Constructor ensures `root` exists (mkdir -p). Callers pass the
    filestore directory directly (e.g. `<data_dir>/filestore`); the
    `filestore/` segment is the caller's responsibility, matching Go's
    New(root) at store.go:20-25 which mkdirs only `root`.
    """

    __slots__ = ("_root", "_mu")

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._mu = threading.Lock()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _hash_from_uri(uri: str) -> str:
        if not uri.startswith(_URI_SCHEME):
            raise ValueError(f"unsupported uri scheme: {uri}")
        hex_ = uri[len(_URI_SCHEME) :]
        if len(hex_) != _HASH_HEX_LEN:
            raise ValueError(f"malformed hash in uri: {uri}")
        return hex_

    def _path_for(self, hex_: str) -> Path:
        return self._root / hex_[:2] / hex_

    def _read_meta(self, meta_path: Path) -> dict[str, object]:
        return json.loads(meta_path.read_text())

    @staticmethod
    def _write_meta(meta_path: Path, refcount: int, mime: str, size: int) -> None:
        meta = {"refcount": refcount, "mime": mime, "size": size}
        meta_path.write_text(json.dumps(meta))

    # -- Protocol methods -------------------------------------------------

    def put(self, content: bytes, mime: str) -> tuple[str, str]:
        """Write content; return (content_uri, sha256_hash).

        Idempotent: re-putting the same content bumps refcount by 1 and
        returns the existing URI. Returns ("file://<hex>", "sha256:<hex>").
        """
        hex_ = hashlib.sha256(content).hexdigest()
        hash_ = f"sha256:{hex_}"
        bucket_dir = self._root / hex_[:2]
        bucket_dir.mkdir(parents=True, exist_ok=True)
        content_path = bucket_dir / hex_
        # Go: metaPath = contentPath + ".meta" — append, do not strip a suffix.
        meta_path = bucket_dir / f"{hex_}.meta"

        with self._mu:
            if content_path.exists():
                # Idempotent: bump refcount on existing blob.
                self._bump_refcount(meta_path, +1)
                return f"file://{hex_}", hash_

            # First write — content then meta. If meta write fails, remove
            # the partial content to avoid leaving an orphan blob.
            content_path.write_bytes(content)
            try:
                self._write_meta(meta_path, 1, mime, len(content))
            except Exception:
                with contextlib.suppress(FileNotFoundError):
                    content_path.unlink()
                raise
            return f"file://{hex_}", hash_

    def get(self, uri: str) -> bytes:
        """Return the content for uri, or raise ExternalizedContentMissing."""
        hex_ = self._hash_from_uri(uri)
        path = self._path_for(hex_)
        if not path.exists():
            raise ExternalizedContentMissing(uri)
        return path.read_bytes()

    def delete(self, uri: str) -> None:
        """Decrement refcount; remove the blob only when refcount hits 0.

        Idempotent: a no-op if the URI is absent (either never put or
        already deleted). This is a Pythonic deviation from Go, whose
        Delete surfaces an error when the meta file is missing — the
        Protocol docstring mandates idempotency, so we treat absent
        meta as "nothing to do".
        """
        hex_ = self._hash_from_uri(uri)
        content_path = self._path_for(hex_)
        meta_path = content_path.with_name(f"{hex_}.meta")

        with self._mu:
            if not meta_path.exists():
                # Either never put, or already deleted. Idempotent no-op.
                # Also covers the case where the content file exists but
                # meta is missing (out-of-band corruption) — we still
                # leave the content file alone, since refcount is unknown.
                return
            meta = self._read_meta(meta_path)
            refcount = int(meta.get("refcount", 0)) - 1
            if refcount > 0:
                self._write_meta(
                    meta_path, refcount, str(meta.get("mime", "")), int(meta.get("size", 0))
                )
                return
            # refcount hit 0 — remove both files. Idempotent on ENOENT.
            with contextlib.suppress(FileNotFoundError):
                content_path.unlink()
            with contextlib.suppress(FileNotFoundError):
                meta_path.unlink()

    # -- private ----------------------------------------------------------

    def _bump_refcount(self, meta_path: Path, delta: int) -> None:
        meta = self._read_meta(meta_path)
        refcount = int(meta.get("refcount", 0)) + delta
        if refcount < 0:
            refcount = 0
        self._write_meta(meta_path, refcount, str(meta.get("mime", "")), int(meta.get("size", 0)))
