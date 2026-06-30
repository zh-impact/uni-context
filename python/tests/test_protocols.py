"""Structural-typing smoke tests for the Protocol interfaces.

Each test defines a minimal stub class implementing one Protocol's
method surface and asserts `isinstance(stub_instance, Protocol)` is
True. These are STRUCTURAL tests only — they verify that the stub's
method set matches the Protocol's, NOT that the methods behave
correctly. Behavioral tests live with each impl in Phase 2.

`@runtime_checkable` makes isinstance() work against a Protocol by
checking attribute presence (no signature inspection at runtime).
A stub missing a method, or with the wrong name, fails the
isinstance check.
"""

from __future__ import annotations

import pytest

from unictx.embed.embedder import Embedder, ModelInfo
from unictx.embed.embedding_repo import EmbeddingRepo, EmbeddingStatus
from unictx.embed.model_registry import ModelDescriptor, ModelRegistry, ModelSpec
from unictx.items.models import AccessGrant, ContextItem, Scope
from unictx.items.repo import AccessRepo, ContextRepo, ItemFilter
from unictx.pdf.extractor import PDFExtractor
from unictx.search.searcher import Searcher, SearchHit, SearchQuery
from unictx.search.vectorstore import VectorHit, VectorQuery, VectorStore
from unictx.storage.filestore import FileStore

# ---------------------------------------------------------------------------
# Stub classes — one per Protocol. Each implements the Protocol's methods
# with no-op/return-None bodies. Method names must match the Protocol
# EXACTLY (snake_case); otherwise the isinstance check fails.
# ---------------------------------------------------------------------------


class _StubContextRepo:
    def create(self, item: ContextItem) -> None:
        pass

    def get(self, id: str) -> ContextItem:
        return ContextItem()

    def update(self, item: ContextItem) -> ContextItem:
        return item

    def delete(self, id: str) -> None:
        pass

    def list(self, filter: ItemFilter) -> tuple[list[ContextItem], str]:
        return ([], "")

    def next_cursor(self, item: ContextItem) -> str:
        return ""

    def reindex_fts(self, id: str, title: str, summary: str, content: str) -> None:
        pass


class _StubSearcher:
    def search_fts(self, q: SearchQuery) -> list[SearchHit]:
        return []

    def search_vector(self, q: VectorQuery) -> list[VectorHit]:
        return []


class _StubVectorStore:
    def put(self, model: str, item_id: str, vector: list[float]) -> None:
        pass

    def search(self, q: VectorQuery) -> list[VectorHit]:
        return []

    def delete(self, model: str, item_id: str) -> None:
        pass


class _StubEmbedder:
    def model(self) -> ModelInfo:
        return ModelInfo()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return []


class _StubEmbeddingRepo:
    def upsert_status(self, item_id: str, model_slug: str, status: str, err_str: str) -> None:
        pass

    def get_status(self, item_id: str, model_slug: str) -> EmbeddingStatus:
        return EmbeddingStatus()

    def list_failed(self, limit: int) -> list[EmbeddingStatus]:
        return []

    def list_for_item(self, item_id: str) -> list[EmbeddingStatus]:
        return []


class _StubModelRegistry:
    def list(self) -> list[ModelDescriptor]:
        return []

    def get_active(self) -> ModelDescriptor:
        return ModelDescriptor()

    def get(self, slug: str) -> ModelDescriptor:
        return ModelDescriptor()

    def register(self, spec: ModelSpec) -> None:
        pass

    def update_config(self, slug: str, base_url: str, api_key: str, provider: str) -> None:
        pass

    def set_default(self, slug: str) -> None:
        pass

    def remove(self, slug: str) -> None:
        pass


class _StubFileStore:
    def put(self, content: bytes, mime: str) -> tuple[str, str]:
        return ("", "")

    def get(self, uri: str) -> bytes:
        return b""

    def delete(self, uri: str) -> None:
        pass


class _StubAccessRepo:
    def list_grants(
        self, as_scope: Scope, as_project_id: str = ""
    ) -> list[AccessGrant]:
        return []

    def grant(self, g: AccessGrant) -> int:
        return 0

    def revoke(self, grant_id: int) -> None:
        pass

    def list_all_grants(
        self,
        as_scope: Scope | None = None,
        as_project_id: str = "",
    ) -> list[tuple[int, AccessGrant]]:
        return []


class _StubPDFExtractor:
    def extract(self, content: bytes) -> str:
        return ""


# ---------------------------------------------------------------------------
# Tests — one per Protocol. ID order documents the test surface.
# ---------------------------------------------------------------------------


def test_context_repo_protocol_is_satisfied() -> None:
    """A class with all ContextRepo methods passes isinstance."""
    assert isinstance(_StubContextRepo(), ContextRepo)


def test_searcher_protocol_is_satisfied() -> None:
    assert isinstance(_StubSearcher(), Searcher)


def test_vector_store_protocol_is_satisfied() -> None:
    assert isinstance(_StubVectorStore(), VectorStore)


def test_embedder_protocol_is_satisfied() -> None:
    assert isinstance(_StubEmbedder(), Embedder)


def test_embedding_repo_protocol_is_satisfied() -> None:
    assert isinstance(_StubEmbeddingRepo(), EmbeddingRepo)


def test_model_registry_protocol_is_satisfied() -> None:
    assert isinstance(_StubModelRegistry(), ModelRegistry)


def test_file_store_protocol_is_satisfied() -> None:
    assert isinstance(_StubFileStore(), FileStore)


def test_pdf_extractor_protocol_is_satisfied() -> None:
    assert isinstance(_StubPDFExtractor(), PDFExtractor)


def test_access_repo_protocol_is_satisfied() -> None:
    assert isinstance(_StubAccessRepo(), AccessRepo)


# ---------------------------------------------------------------------------
# Negative test — confirm runtime_checkable actually checks method presence.
# A class missing one method must fail the isinstance check. This guards
# against accidental Protocol signature drift (rename a method in the
# Protocol without updating impls and this test catches it).
# ---------------------------------------------------------------------------


def test_runtime_checkable_rejects_missing_method() -> None:
    class _StubMissingMethod:
        # Only implements `get`; ContextRepo requires 7 methods.
        def get(self, id: str) -> ContextItem:
            return ContextItem()

    with pytest.raises(AssertionError):
        assert isinstance(_StubMissingMethod(), ContextRepo)
