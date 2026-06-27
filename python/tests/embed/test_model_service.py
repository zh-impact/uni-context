"""Tests for ModelService — embedding model lifecycle + status rows.

Mirrors Go's model_test.go. ModelService is a thin pass-through over
ModelRegistry + EmbeddingRepo; tests verify the boundary contract
( each method delegates with the right arguments) rather than
re-testing registry/repo internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from unictx.embed.embedding_repo import EmbeddingStatus
from unictx.embed.model_registry import ModelDescriptor, ModelSpec
from unictx.embed.model_service import ModelService

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RecordingRegistry:
    """Records register/list/remove/set_default calls for assertion."""
    register_calls: list[ModelSpec] = field(default_factory=list)
    remove_calls: list[str] = field(default_factory=list)
    set_default_calls: list[str] = field(default_factory=list)
    list_return: list[ModelDescriptor] = field(default_factory=list)

    def list(self): return list(self.list_return)
    def get_active(self): raise NotImplementedError
    def get(self, slug): raise NotImplementedError
    def register(self, spec: ModelSpec) -> None:
        self.register_calls.append(spec)
    def update_config(self, *args): raise NotImplementedError
    def set_default(self, slug: str) -> None:
        self.set_default_calls.append(slug)
    def remove(self, slug: str) -> None:
        self.remove_calls.append(slug)


@dataclass(slots=True)
class _RecordingEmbRepo:
    """Records list_for_item calls."""
    list_for_item_calls: list[str] = field(default_factory=list)
    list_for_item_return: list[EmbeddingStatus] = field(default_factory=list)

    def list_for_item(self, item_id):
        self.list_for_item_calls.append(item_id)
        return list(self.list_for_item_return)

    # Unused by ModelService; declared for Protocol satisfaction.
    def upsert_status(self, *args): raise NotImplementedError
    def list_failed(self, limit): raise NotImplementedError
    def get_status(self, item_id, model_slug): raise NotImplementedError


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_add_model_delegates_to_registry() -> None:
    """add_model(spec) forwards to ModelRegistry.register."""
    reg = _RecordingRegistry()
    svc = ModelService(reg, _RecordingEmbRepo())
    spec = ModelSpec(slug="ollama", provider="ollama", base_url="http://x", dimension=8)

    svc.add_model(spec)

    assert reg.register_calls == [spec]


def test_list_models_delegates_to_registry() -> None:
    """list_models() forwards to ModelRegistry.list."""
    preset = [ModelDescriptor(slug="a", dimension=8),
              ModelDescriptor(slug="b", dimension=8)]
    reg = _RecordingRegistry(list_return=preset)
    svc = ModelService(reg, _RecordingEmbRepo())

    result = svc.list_models()

    assert result == preset


def test_remove_model_delegates_to_registry() -> None:
    """remove_model(slug) forwards to ModelRegistry.remove."""
    reg = _RecordingRegistry()
    svc = ModelService(reg, _RecordingEmbRepo())

    svc.remove_model("ollama")

    assert reg.remove_calls == ["ollama"]


def test_switch_model_delegates_to_registry() -> None:
    """switch_model(slug) forwards to ModelRegistry.set_default."""
    reg = _RecordingRegistry()
    svc = ModelService(reg, _RecordingEmbRepo())

    svc.switch_model("newmodel")

    assert reg.set_default_calls == ["newmodel"]


def test_item_embedding_status_delegates_to_emb_repo() -> None:
    """item_embedding_status(id) forwards to EmbeddingRepo.list_for_item."""
    preset = [EmbeddingStatus(item_id="i-1", model_slug="m", status="done")]
    emb_repo = _RecordingEmbRepo(list_for_item_return=preset)
    svc = ModelService(_RecordingRegistry(), emb_repo)

    result = svc.item_embedding_status("i-1")

    assert result == preset
    assert emb_repo.list_for_item_calls == ["i-1"]


def test_item_embedding_status_empty_when_no_rows() -> None:
    """No status rows → empty list (not None)."""
    emb_repo = _RecordingEmbRepo(list_for_item_return=[])
    svc = ModelService(_RecordingRegistry(), emb_repo)

    result = svc.item_embedding_status("ghost")

    assert result == []
