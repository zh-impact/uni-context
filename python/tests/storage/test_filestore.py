"""Tests for unictx.storage.filestore — sha256 content-addressed blob store.

Ports Go's internal/adapter/fsstore/store_test.go. Two cross-compat tests:
  - test_layout_matches_go_convention runs always (proves path layout).
  - test_get_go_written_blob is gated on a Go filestore existing
    (developer smoke test, not CI).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from unictx.items.errors import ExternalizedContentMissing
from unictx.storage.filestore import FileStore, FileStoreImpl

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_protocol_conformance(tmp_path: Path) -> None:
    """FileStoreImpl must satisfy the FileStore runtime-checkable Protocol."""
    s = FileStoreImpl(tmp_path / "fs")
    assert isinstance(s, FileStore)


def test_constructor_creates_root(tmp_path: Path) -> None:
    """Constructor must create the root directory if missing."""
    root = tmp_path / "fs" / "nested" / "deep"
    assert not root.exists()
    FileStoreImpl(root)
    assert root.is_dir()


# ---------------------------------------------------------------------------
# put / get roundtrip
# ---------------------------------------------------------------------------


def test_put_get_roundtrip(tmp_path: Path) -> None:
    """put then get returns identical bytes."""
    s = FileStoreImpl(tmp_path / "fs")
    content = b"hello world this is a test"

    uri, hash_ = s.put(content, "text/plain")

    assert uri.startswith("file://")
    assert hash_.startswith("sha256:")
    expected_hex = hashlib.sha256(content).hexdigest()
    assert uri == f"file://{expected_hex}"
    assert hash_ == f"sha256:{expected_hex}"

    got = s.get(uri)
    assert got == content


def test_put_dedupes_same_content(tmp_path: Path) -> None:
    """Putting identical content twice returns the same URI and bumps refcount."""
    s = FileStoreImpl(tmp_path / "fs")
    content = b"same content same hash"

    uri1, hash1 = s.put(content, "text/plain")
    uri2, hash2 = s.put(content, "text/plain")

    assert uri1 == uri2
    assert hash1 == hash2

    # Exactly one content file on disk for this hash.
    hex_ = hashlib.sha256(content).hexdigest()
    matches = list((tmp_path / "fs" / hex_[:2]).glob(hex_))
    assert len(matches) == 1

    # Meta file shows refcount = 2.
    meta_path = tmp_path / "fs" / hex_[:2] / f"{hex_}.meta"
    meta = json.loads(meta_path.read_text())
    assert meta == {"refcount": 2, "mime": "text/plain", "size": len(content)}


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_with_refcount_above_zero_keeps_file(tmp_path: Path) -> None:
    """When refcount > 1, delete only decrements — content file stays."""
    s = FileStoreImpl(tmp_path / "fs")
    content = b"refcounted"
    uri, _ = s.put(content, "text/plain")
    s.put(content, "text/plain")  # refcount = 2

    s.delete(uri)

    # File still present, refcount = 1.
    got = s.get(uri)
    assert got == content
    hex_ = hashlib.sha256(content).hexdigest()
    meta = json.loads((tmp_path / "fs" / hex_[:2] / f"{hex_}.meta").read_text())
    assert meta["refcount"] == 1


def test_delete_to_zero_removes_file_and_meta(tmp_path: Path) -> None:
    """When refcount reaches 0, both content and meta are removed."""
    s = FileStoreImpl(tmp_path / "fs")
    content = b"to be deleted"
    uri, _ = s.put(content, "text/plain")

    s.delete(uri)

    hex_ = hashlib.sha256(content).hexdigest()
    content_path = tmp_path / "fs" / hex_[:2] / hex_
    meta_path = tmp_path / "fs" / hex_[:2] / f"{hex_}.meta"
    assert not content_path.exists()
    assert not meta_path.exists()


def test_delete_idempotent_on_missing_uri(tmp_path: Path) -> None:
    """Delete on a URI whose content was already removed is a no-op."""
    s = FileStoreImpl(tmp_path / "fs")
    content = b"ephemeral"
    uri, _ = s.put(content, "text/plain")
    s.delete(uri)

    # Second delete must not raise.
    s.delete(uri)
    s.delete(uri)


def test_delete_on_never_existed_uri(tmp_path: Path) -> None:
    """Delete on a URI never put must be idempotent (Go-compatible).

    Go's Delete parses the URI, then readMeta fails with ENOENT — that
    surfaces as an error in Go. Python treats absent meta as a missing
    blob and treats the call as a no-op (Pythonic delete idempotency,
    matching the FileStore.delete docstring "No-op (idempotent) if uri
    absent"). See report for the deviation rationale.
    """
    s = FileStoreImpl(tmp_path / "fs")
    hex_ = "a" * 64
    s.delete(f"file://{hex_}")  # must not raise


# ---------------------------------------------------------------------------
# error cases
# ---------------------------------------------------------------------------


def test_get_missing_raises_externalized_content_missing(tmp_path: Path) -> None:
    """get on a well-formed URI with no blob raises ExternalizedContentMissing."""
    s = FileStoreImpl(tmp_path / "fs")
    hex_ = "a" * 64
    with pytest.raises(ExternalizedContentMissing, match="file://"):
        s.get(f"file://{hex_}")


def test_get_after_delete_raises_externalized_content_missing(tmp_path: Path) -> None:
    """get on a previously-deleted URI raises ExternalizedContentMissing."""
    s = FileStoreImpl(tmp_path / "fs")
    uri, _ = s.put(b"temporary", "text/plain")
    s.delete(uri)
    with pytest.raises(ExternalizedContentMissing):
        s.get(uri)


def test_get_wrong_scheme_raises_value_error(tmp_path: Path) -> None:
    """URI without file:// prefix is rejected."""
    s = FileStoreImpl(tmp_path / "fs")
    with pytest.raises(ValueError, match="scheme"):
        s.get("http://example.com/foo")


def test_get_wrong_hash_length_raises_value_error(tmp_path: Path) -> None:
    """URI whose path is not 64 hex chars is rejected."""
    s = FileStoreImpl(tmp_path / "fs")
    with pytest.raises(ValueError, match="malformed"):
        s.get("file://short")


# ---------------------------------------------------------------------------
# Layout cross-compat (always runs)
# ---------------------------------------------------------------------------


def test_layout_matches_go_convention(tmp_path: Path) -> None:
    """On-disk paths match what Go's fsstore would produce.

    Go layout: <root>/<hex[:2]>/<hex> for content,
                <root>/<hex[:2]>/<hex>.meta for meta JSON.
    Meta JSON shape: {"refcount", "mime", "size"}.
    """
    root = tmp_path / "fs"
    s = FileStoreImpl(root)
    content = b"layout matters"
    uri, _ = s.put(content, "application/octet-stream")

    hex_ = hashlib.sha256(content).hexdigest()
    content_path = root / hex_[:2] / hex_
    meta_path = root / hex_[:2] / f"{hex_}.meta"

    assert content_path.is_file()
    assert meta_path.is_file()
    assert content_path.read_bytes() == content

    meta = json.loads(meta_path.read_text())
    assert set(meta.keys()) == {"refcount", "mime", "size"}
    assert meta["refcount"] == 1
    assert meta["mime"] == "application/octet-stream"
    assert meta["size"] == len(content)


# ---------------------------------------------------------------------------
# Go cross-compat (skipif no Go filestore)
# ---------------------------------------------------------------------------

_GO_FS_ROOT = Path.home() / ".local" / "share" / "unictx" / "filestore"


@pytest.mark.skipif(
    not _GO_FS_ROOT.is_dir(),
    reason="no Go filestore on this machine (developer smoke test, not CI)",
)
def test_get_go_written_blob() -> None:
    """Read a Go-written blob through Python's FileStore.

    Lists the first Go-written blob under the filestore root, reads it
    via FileStoreImpl.get(uri), and verifies its content hashes to the
    URI's digest. This is the load-bearing cross-compat proof.
    """
    s = FileStoreImpl(_GO_FS_ROOT)
    # Walk to the first content file (skip *.meta).
    for bucket in sorted(_GO_FS_ROOT.iterdir()):
        if not bucket.is_dir():
            continue
        for entry in sorted(bucket.iterdir()):
            if entry.suffix == ".meta" or entry.name.startswith("."):
                continue
            hex_ = entry.name
            if len(hex_) != 64:
                continue
            uri = f"file://{hex_}"
            got = s.get(uri)
            assert hashlib.sha256(got).hexdigest() == hex_
            return
    pytest.skip("filestore directory exists but contains no blobs")
