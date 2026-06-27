"""Tests for cli/doctor.py — doctor command + helper."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import unictx.cli.doctor as doctor_mod
from unictx.cli.app import app as root_app
from unictx.cli.doctor import format_doctor_line
from unictx.config import Config, EmbedderConfig
from unictx.embed.embedder import ModelInfo


@pytest.fixture
def container_factory(tmp_path: Path, monkeypatch):
    """Wire a per-call container; monkeypatch into doctor module."""
    created: list = []

    def _make():
        cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
        c = doctor_mod.wire(cfg)
        created.append(c)
        return c

    monkeypatch.setattr(doctor_mod, "_load_container", _make)
    yield _make
    for c in created:
        c.close()


# ---------------------------------------------------------------------------
# format_doctor_line
# ---------------------------------------------------------------------------


def test_format_doctor_line_pads_label_to_15() -> None:
    """Labels shorter than 15 chars get padded; value follows after one space."""
    line = format_doctor_line("status:", "OK")
    # Label "status:" is 7 chars; padded to 15, then " OK".
    assert line == "status:         OK"


def test_format_doctor_line_long_label_no_truncate() -> None:
    """Labels longer than 15 chars take their full width + space."""
    line = format_doctor_line("schema version:", "0001")
    # 15-char label fits exactly; format spec {label:<15} on a 15-char
    # label is a no-op.
    assert line == "schema version: 0001"


# ---------------------------------------------------------------------------
# End-to-end via CliRunner
# ---------------------------------------------------------------------------


def test_doctor_disabled_embedder_reports_status_ok(container_factory) -> None:
    """Plan 1 (embedder disabled): doctor prints 'disabled' + status OK."""
    runner = CliRunner()
    result = runner.invoke(root_app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "schema version:" in result.output
    assert "embedder: disabled" in result.output
    assert "status:" in result.output
    assert "OK" in result.output


def test_doctor_json_output(container_factory) -> None:
    """--json → structured payload with status + embedder state."""
    import json as _json

    runner = CliRunner()
    result = runner.invoke(root_app, ["--json", "doctor"])
    assert result.exit_code == 0, result.output

    out = result.output.strip()
    brace = out.index("{")
    payload = _json.loads(out[brace:])
    assert payload["status"] == "OK"
    assert payload["embedder"]["state"] == "disabled"
    assert "schema_version" in payload
    assert "data_dir" in payload


def test_doctor_embedder_fail_returns_nonzero(tmp_path: Path, monkeypatch) -> None:
    """When ping_embedder raises, doctor prints FAIL + exits non-zero."""
    created: list = []

    def _make():
        cfg = Config(data_dir=tmp_path, embedder=EmbedderConfig(enabled=False))
        c = doctor_mod.wire(cfg)
        created.append(c)
        return c

    monkeypatch.setattr(doctor_mod, "_load_container", _make)

    # Patch DiagnosticService.ping_embedder to raise.
    from unictx.embed.diagnostic import DiagnosticService

    def _boom(self):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(DiagnosticService, "ping_embedder", _boom)
    try:
        runner = CliRunner()
        result = runner.invoke(root_app, ["doctor"])
        assert result.exit_code != 0
        assert "FAIL" in result.output
        assert "ollama unreachable" in result.output
    finally:
        for c in created:
            c.close()


def test_doctor_enabled_embedder_reports_ok(tmp_path: Path, monkeypatch) -> None:
    """When embedder enabled and ping succeeds, doctor prints embedder: OK."""
    # Use a per-call tmp to let the bge-m3 seed run.
    counter = [0]
    created: list = []

    def _make():
        counter[0] += 1
        sub = tmp_path / f"c{counter[0]}"
        sub.mkdir()
        cfg = Config(
            data_dir=sub,
            embedder=EmbedderConfig(
                enabled=True,
                provider="ollama",
                base_url="http://localhost:11434",
                model="bge-m3",
                dimension=1024,
            ),
        )
        c = doctor_mod.wire(cfg)
        created.append(c)
        return c

    monkeypatch.setattr(doctor_mod, "_load_container", _make)

    # Patch ping_embedder so we don't hit real Ollama.
    from unictx.embed.diagnostic import DiagnosticService

    def _fake_ping(self):
        return ModelInfo(slug="bge-m3", dimension=1024), True

    monkeypatch.setattr(DiagnosticService, "ping_embedder", _fake_ping)
    try:
        runner = CliRunner()
        result = runner.invoke(root_app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "embedder: OK" in result.output
        assert "bge-m3" in result.output
        assert "1024-dim" in result.output
    finally:
        for c in created:
            c.close()
