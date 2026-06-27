"""Tests for ItemService — get/list/delete with externalization hydration.

Mirrors Go's item_test.go. Tests exercise: hydrate inline content
(no fs hit), hydrate externalized (fs.get called), title-only (no
hydration), list pass-through, delete pass-through, fs miss raises.
"""

from __future__ import annotations

import pytest

from tests._fakes.canned_filestore import CannedFileStore
from tests._fakes.fake_repo import FakeContextRepo
from unictx.items.errors import ExternalizedContentMissing, ItemNotFound
from unictx.items.item_service import ItemService
from unictx.items.models import ContextItem, Kind, Scope, Source
from unictx.items.repo import ItemFilter


def _make_item(
    item_id: str, *, title: str = "", content: str = "", content_uri: str = ""
) -> ContextItem:
    return ContextItem(
        id=item_id,
        scope=Scope.USER, kind=Kind.NOTE, source=Source.MANUAL,
        owner_user_id="u-1",
        title=title, content=content, content_uri=content_uri,
    )


def test_get_returns_inline_content_without_fs_hit() -> None:
    """Item with inline content → returned as-is; FileStore untouched."""
    repo = FakeContextRepo()
    fs = CannedFileStore()
    svc = ItemService(repo, fs)
    item = _make_item("i-1", title="t", content="inline body")
    repo.create(item)

    got = svc.get("i-1")
    assert got.content == "inline body"
    assert len(fs.data) == 0, "fs not touched for inline content"


def test_get_hydrates_externalized_from_filestore() -> None:
    """Item with content_uri (no inline) → fs.get fills content."""
    repo = FakeContextRepo()
    fs = CannedFileStore()
    svc = ItemService(repo, fs)
    uri, _ = fs.put(b"external body", "text/plain")
    item = _make_item("i-1", title="t", content="", content_uri=uri)
    repo.create(item)

    got = svc.get("i-1")
    assert got.content == "external body"
    assert got.content_uri == uri, "content_uri preserved"


def test_get_title_only_returns_empty_content() -> None:
    """No content + no content_uri → content="" (title-only)."""
    repo = FakeContextRepo()
    svc = ItemService(repo, CannedFileStore())
    repo.create(_make_item("i-1", title="just a title"))

    got = svc.get("i-1")
    assert got.content == ""
    assert got.title == "just a title"


def test_get_missing_item_raises_not_found() -> None:
    """repo.get error propagates unwrapped (so caller can distinguish)."""
    repo = FakeContextRepo()
    svc = ItemService(repo, CannedFileStore())

    with pytest.raises(ItemNotFound):
        svc.get("ghost")


def test_get_fs_miss_raises_with_uri_context() -> None:
    """fs.get failure on dangling content_uri → raises with uri context."""
    repo = FakeContextRepo()
    fs = CannedFileStore()
    svc = ItemService(repo, fs)
    # Item references a URI not in the FileStore.
    repo.create(_make_item("i-1", content_uri="file://dangling"))

    with pytest.raises(ExternalizedContentMissing):
        svc.get("i-1")


def test_list_passes_filter_through() -> None:
    """list(filter) delegates to repo.list with the same filter."""
    repo = FakeContextRepo()
    svc = ItemService(repo, CannedFileStore())
    repo.create(_make_item("a"))
    repo.create(_make_item("b"))

    rows, next_cursor = svc.list(ItemFilter(limit=10))

    assert len(rows) == 2
    assert next_cursor == "", "FakeContextRepo returns empty cursor"


def test_delete_delegates_to_repo() -> None:
    """delete(id) removes the item from repo."""
    repo = FakeContextRepo()
    svc = ItemService(repo, CannedFileStore())
    repo.create(_make_item("i-1"))

    svc.delete("i-1")

    with pytest.raises(ItemNotFound):
        repo.get("i-1")
