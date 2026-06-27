"""Tests for DiagnosticService — schema_version + ping_embedder.

Mirrors Go's diagnostic_test.go. Tests exercise: schema_version
pass-through, ping_embedder disabled (no embedder), ping success
(returns ModelInfo), ping failure (raises embedder error).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tests._fakes.fake_embedder import ErrorEmbedder, FakeEmbedder
from unictx.embed.diagnostic import DiagnosticService
from unictx.embed.embedder import ModelInfo


@dataclass(slots=True)
class _StubSchemaMeta:
    """Stub SchemaMeta returning a preset version (or raising)."""
    version_value: str = "4"
    err: Exception | None = None

    def version(self) -> str:
        if self.err is not None:
            raise self.err
        return self.version_value


# ---------------------------------------------------------------------------
# schema_version
# ---------------------------------------------------------------------------


def test_schema_version_passes_through() -> None:
    """schema_version() forwards to the SchemaMeta impl."""
    schema = _StubSchemaMeta(version_value="7")
    svc = DiagnosticService(schema)
    assert svc.schema_version() == "7"


def test_schema_version_propagates_errors() -> None:
    """SchemaMeta error surfaces unwrapped."""
    schema = _StubSchemaMeta(err=RuntimeError("schema_meta table missing"))
    svc = DiagnosticService(schema)
    with pytest.raises(RuntimeError, match=r"schema_meta table missing"):
        svc.schema_version()


# ---------------------------------------------------------------------------
# ping_embedder
# ---------------------------------------------------------------------------


def test_ping_embedder_disabled_when_no_embedder() -> None:
    """No embedder → returns (zero, False) — Plan 1 behavior."""
    schema = _StubSchemaMeta()
    svc = DiagnosticService(schema, embedder=None)

    info, enabled = svc.ping_embedder()

    assert enabled is False
    assert info == ModelInfo(), "disabled returns zero ModelInfo"


def test_ping_embedder_success_returns_model_info() -> None:
    """Embedder answers the ping → returns its ModelInfo + enabled=True."""
    schema = _StubSchemaMeta()
    emb = FakeEmbedder(dimension=8, model_info=ModelInfo(slug="ollama", dimension=8))
    svc = DiagnosticService(schema, embedder=emb)

    info, enabled = svc.ping_embedder()

    assert enabled is True
    assert info.slug == "ollama"
    assert info.dimension == 8
    # The ping text was embedded (FakeEmbedder records calls).
    assert emb.embed_calls == [["ping"]]


def test_ping_embedder_failure_raises() -> None:
    """Embedder error raises — caller wraps in try/except for FAIL branch.

    Verifies the Go-faithful behavior: model() is NOT called on failure
    (avoids masking the embed error with a stale model label).
    """
    schema = _StubSchemaMeta()
    inner = FakeEmbedder(model_info=ModelInfo(slug="stale", dimension=8))
    err_emb = ErrorEmbedder(inner=inner, reason="connection refused")
    svc = DiagnosticService(schema, embedder=err_emb)

    with pytest.raises(Exception, match=r"connection refused"):
        svc.ping_embedder()
