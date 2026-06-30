"""Smoke tests for shared fakes in tests/_fakes/.

Each test asserts two things (mirrors tests/test_protocols.py pattern):
  1. isinstance(fake_instance, Protocol) is True — the fake satisfies
     its Protocol structurally (method set matches).
  2. isinstance(non_conforming_object, Protocol) is False — negative
     control proving runtime_checkable is actually checking methods,
     not rubber-stamping everything.

These are STRUCTURAL tests only. Behavioral tests for the fakes live
inline with the service tests that exercise them (Phase 5).
"""

from __future__ import annotations

import hashlib

import pytest

from tests._fakes.canned_filestore import CannedFileStore
from tests._fakes.fake_access_repo import FakeAccessRepo
from tests._fakes.fake_embedder import ErrorEmbedder, FakeEmbedder
from tests._fakes.fake_repo import FakeContextRepo
from unictx.embed.embedder import Embedder, ModelInfo
from unictx.embed.errors import EmbeddingFailed
from unictx.items.errors import ExternalizedContentMissing, ItemNotFound, ItemValidationError
from unictx.items.models import AccessGrant, Kind, NewItemParams, Scope, Source, new_context_item
from unictx.items.repo import AccessRepo, ContextRepo, ItemFilter
from unictx.storage.filestore import FileStore

# ---------------------------------------------------------------------------
# Protocol-conformance smoke tests (structural)
# ---------------------------------------------------------------------------


def test_fake_repo_satisfies_context_repo_protocol() -> None:
    """FakeContextRepo structurally conforms to ContextRepo."""
    assert isinstance(FakeContextRepo(), ContextRepo)

    class _MissingMethods:
        pass

    assert not isinstance(_MissingMethods(), ContextRepo)


def test_canned_filestore_satisfies_filestore_protocol() -> None:
    """CannedFileStore structurally conforms to FileStore."""
    assert isinstance(CannedFileStore(), FileStore)

    class _MissingMethods:
        pass

    assert not isinstance(_MissingMethods(), FileStore)


def test_fake_embedder_satisfies_embedder_protocol() -> None:
    """FakeEmbedder structurally conforms to Embedder."""
    assert isinstance(FakeEmbedder(), Embedder)

    class _MissingMethods:
        pass

    assert not isinstance(_MissingMethods(), Embedder)


def test_error_embedder_satisfies_embedder_protocol() -> None:
    """ErrorEmbedder structurally conforms to Embedder."""
    assert isinstance(ErrorEmbedder(), Embedder)


# ---------------------------------------------------------------------------
# Behavioral smoke tests — one assertion per fake to prove the wiring
# works (not exhaustive; full coverage comes from service tests in Phase 5).
# ---------------------------------------------------------------------------


def test_fake_repo_records_reindex_fts_calls() -> None:
    """reindex_fts increments the call counter and records args."""
    repo = FakeContextRepo()
    repo.reindex_fts("id1", "t", "s", "c")
    repo.reindex_fts("id2", "t2", "s2", "c2")
    assert repo.reindex_fts_calls == 2
    assert repo.reindex_fts_args == [
        ("id1", "t", "s", "c"),
        ("id2", "t2", "s2", "c2"),
    ]


def test_fake_repo_get_missing_raises_item_not_found() -> None:
    repo = FakeContextRepo()
    with pytest.raises(ItemNotFound):
        repo.get("nope")


def test_fake_repo_create_duplicate_raises_validation() -> None:
    repo = FakeContextRepo()
    item = new_context_item(Scope.USER, Kind.NOTE, Source.MANUAL, NewItemParams(owner_user_id="u"))
    repo.create(item)
    with pytest.raises(ItemValidationError):
        repo.create(item)


def test_canned_filestore_records_deletes() -> None:
    """delete() appends to deleted_uris and removes the URI from dict."""
    fs = CannedFileStore(data={"file://a": b"a", "file://b": b"b"})
    fs.delete("file://a")
    fs.delete("file://never-existed")  # idempotent — still recorded
    assert fs.deleted_uris == ["file://a", "file://never-existed"]
    assert "file://a" not in fs.data
    assert "file://b" in fs.data  # untouched


def test_canned_filestore_get_missing_raises_externalized_missing() -> None:
    fs = CannedFileStore()
    with pytest.raises(ExternalizedContentMissing):
        fs.get("file://unknown")


def test_canned_filestore_put_round_trips_via_sha256() -> None:
    """put returns a sha256 URI that get can resolve back to content."""
    fs = CannedFileStore()
    uri, h = fs.put(b"hello", "text/plain")
    assert h == hashlib.sha256(b"hello").hexdigest()
    assert fs.get(uri) == b"hello"


def test_fake_embedder_is_deterministic() -> None:
    """Same input -> same output. Different inputs -> different vectors."""
    emb = FakeEmbedder(dimension=8)
    a1 = emb.embed(["hello"])[0]
    a2 = emb.embed(["hello"])[0]
    b1 = emb.embed(["world"])[0]
    assert a1 == a2
    assert a1 != b1
    assert len(a1) == 8
    # embed_calls records each call's input list
    assert emb.embed_calls == [["hello"], ["hello"], ["world"]]


def test_fake_embedder_default_model_info() -> None:
    emb = FakeEmbedder(dimension=16)
    assert emb.model() == ModelInfo(slug="fake", dimension=16)


def test_error_embedder_always_raises_embedding_failed() -> None:
    """ErrorEmbedder.embed raises EmbeddingFailed carrying the model slug."""
    inner = FakeEmbedder(dimension=4, model_info=ModelInfo(slug="boom", dimension=4))
    err_emb = ErrorEmbedder(inner=inner)
    # model() still works — Phase 5 search tests assert slug in the
    # degraded path even when embed fails.
    assert err_emb.model().slug == "boom"
    with pytest.raises(EmbeddingFailed) as exc_info:
        err_emb.embed(["anything"])
    assert exc_info.value.slug == "boom"


def test_fake_repo_list_returns_all_ignoring_filter() -> None:
    """list() ignores ItemFilter, matching Go's fakeRepo.List behavior."""
    repo = FakeContextRepo()
    item1 = new_context_item(Scope.USER, Kind.NOTE, Source.MANUAL, NewItemParams(owner_user_id="u"))
    item2 = new_context_item(
        Scope.USER, Kind.NOTE, Source.MANUAL, NewItemParams(owner_user_id="u2")
    )
    repo.create(item1)
    repo.create(item2)
    rows, cursor = repo.list(ItemFilter())
    assert {r.id for r in rows} == {item1.id, item2.id}
    assert cursor == ""


def test_fake_repo_update_and_delete_round_trip() -> None:
    """update writes through; delete removes; missing-id raises."""
    repo = FakeContextRepo()
    item = new_context_item(Scope.USER, Kind.NOTE, Source.MANUAL, NewItemParams(owner_user_id="u"))
    repo.create(item)
    item.title = "updated"
    updated = repo.update(item)
    assert updated.title == "updated"
    assert repo.get(item.id).title == "updated"
    repo.delete(item.id)
    with pytest.raises(ItemNotFound):
        repo.get(item.id)
    with pytest.raises(ItemNotFound):
        repo.update(item)
    with pytest.raises(ItemNotFound):
        repo.delete(item.id)


def test_fake_access_repo_satisfies_access_repo_protocol() -> None:
    """FakeAccessRepo structurally conforms to AccessRepo."""
    assert isinstance(FakeAccessRepo(), AccessRepo)

    class _MissingMethods:
        pass

    assert not isinstance(_MissingMethods(), AccessRepo)


def test_fake_access_repo_filters_by_as_scope_and_project() -> None:
    """list_grants filters by as_scope, and project_id matches "all" or exact."""
    repo = FakeAccessRepo(
        grants=[
            # Applies to all projects acting as project.
            AccessGrant(as_scope=Scope.PROJECT, project_id="", target_scope=Scope.USER),
            # Applies only to project P.
            AccessGrant(as_scope=Scope.PROJECT, project_id="P", target_scope=Scope.GLOBAL),
            # Wrong as_scope — never matches a project query.
            AccessGrant(as_scope=Scope.GLOBAL, project_id="", target_scope=Scope.USER),
        ]
    )

    # Project P sees both the all-projects grant and its own.
    grants_p = repo.list_grants(Scope.PROJECT, "P")
    assert {g.target_scope for g in grants_p} == {Scope.USER, Scope.GLOBAL}

    # Project Q sees only the all-projects grant (not P-specific).
    grants_q = repo.list_grants(Scope.PROJECT, "Q")
    assert [g.target_scope for g in grants_q] == [Scope.USER]

    # Global actor sees only its own-scope grant.
    grants_global = repo.list_grants(Scope.GLOBAL)
    assert [g.target_scope for g in grants_global] == [Scope.USER]
