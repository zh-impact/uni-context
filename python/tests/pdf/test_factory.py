"""Tests for the PDF extractor factory.

Verifies engine selection, default-handling, and misconfiguration errors
for the fitz/shell/http trio. Mirrors Go's pdf_test.go behavior.

Cases:
  - Disabled (engine="") → None
  - Fitz engine selection
  - Shell engine: default config, custom config, empty command → error
  - HTTP engine: default config, custom config, empty url → error
  - Unknown engine → error
  - build_extractor_for_engine happy + error paths
  - All returned extractors satisfy PDFExtractor Protocol
"""

from __future__ import annotations

import pytest

from unictx.config import (
    HttpPdfEngineConfig,
    PdfConfig,
    PdfEnginesConfig,
    ShellPdfEngineConfig,
)
from unictx.pdf.extractor import PDFExtractor
from unictx.pdf.factory import build_extractor_for_engine, build_pdf_extractor
from unictx.pdf.fitz_engine import FitzEngine
from unictx.pdf.http_engine import HttpEngine
from unictx.pdf.shell_engine import ShellEngine

# ---------------------------------------------------------------------------
# Disabled
# ---------------------------------------------------------------------------


def test_disabled_returns_none() -> None:
    """Empty engine name → None — caller proceeds without PDF support."""
    assert build_pdf_extractor(PdfConfig(engine="")) is None


def test_disabled_default_config_returns_none() -> None:
    """Default PdfConfig has engine="" → None."""
    assert build_pdf_extractor(PdfConfig()) is None


# ---------------------------------------------------------------------------
# Fitz engine
# ---------------------------------------------------------------------------


def test_fitz_engine_selection() -> None:
    ext = build_pdf_extractor(PdfConfig(engine="fitz"))
    assert isinstance(ext, FitzEngine)
    assert isinstance(ext, PDFExtractor)


# ---------------------------------------------------------------------------
# Shell engine
# ---------------------------------------------------------------------------


def test_shell_engine_default_command() -> None:
    """Default ShellPdfEngineConfig.command is 'pdftotext - -' — non-empty."""
    ext = build_pdf_extractor(PdfConfig(engine="shell"))
    assert isinstance(ext, ShellEngine)
    assert isinstance(ext, PDFExtractor)


def test_shell_engine_custom_command_and_timeout() -> None:
    cfg = PdfConfig(
        engine="shell",
        engines=PdfEnginesConfig(
            shell=ShellPdfEngineConfig(
                command="mutool draw -F txt", timeout_seconds=15
            ),
        ),
    )
    ext = build_pdf_extractor(cfg)
    assert isinstance(ext, ShellEngine)
    assert ext._command == "mutool draw -F txt"
    assert ext._timeout == 15.0


def test_shell_engine_empty_command_raises() -> None:
    cfg = PdfConfig(
        engine="shell",
        engines=PdfEnginesConfig(
            shell=ShellPdfEngineConfig(command=""),
        ),
    )
    with pytest.raises(ValueError, match=r"shell.*pdf\.engines\.shell\.command") as exc_info:
        build_pdf_extractor(cfg)
    msg = str(exc_info.value)
    assert "shell" in msg
    assert "pdf.engines.shell.command" in msg


# ---------------------------------------------------------------------------
# HTTP engine
# ---------------------------------------------------------------------------


def test_http_engine_default_url() -> None:
    """Default HttpPdfEngineConfig.url is non-empty."""
    ext = build_pdf_extractor(PdfConfig(engine="http"))
    assert isinstance(ext, HttpEngine)
    assert isinstance(ext, PDFExtractor)


def test_http_engine_custom_url_timeout_token() -> None:
    cfg = PdfConfig(
        engine="http",
        engines=PdfEnginesConfig(
            http=HttpPdfEngineConfig(
                url="https://example.com/extract",
                timeout_seconds=45,
                auth_token="secret",
            ),
        ),
    )
    ext = build_pdf_extractor(cfg)
    assert isinstance(ext, HttpEngine)
    assert ext._url == "https://example.com/extract"
    assert ext._timeout == 45.0
    assert ext._auth_token == "secret"


def test_http_engine_empty_url_raises() -> None:
    cfg = PdfConfig(
        engine="http",
        engines=PdfEnginesConfig(
            http=HttpPdfEngineConfig(url=""),
        ),
    )
    with pytest.raises(ValueError, match=r"http.*pdf\.engines\.http\.url") as exc_info:
        build_pdf_extractor(cfg)
    msg = str(exc_info.value)
    assert "http" in msg
    assert "pdf.engines.http.url" in msg


# ---------------------------------------------------------------------------
# Unknown engine
# ---------------------------------------------------------------------------


def test_unknown_engine_raises() -> None:
    with pytest.raises(ValueError, match=r"unknown pdf engine") as exc_info:
        build_pdf_extractor(PdfConfig(engine="ocr-magic"))
    msg = str(exc_info.value)
    assert "ocr-magic" in msg
    # Error must list valid options so the user knows what to type.
    assert "fitz" in msg
    assert "shell" in msg
    assert "http" in msg


# ---------------------------------------------------------------------------
# build_extractor_for_engine
# ---------------------------------------------------------------------------


def test_build_extractor_for_engine_fitz() -> None:
    ext = build_extractor_for_engine("fitz", PdfConfig())
    assert isinstance(ext, FitzEngine)


def test_build_extractor_for_engine_shell() -> None:
    ext = build_extractor_for_engine("shell", PdfConfig())
    assert isinstance(ext, ShellEngine)


def test_build_extractor_for_engine_http() -> None:
    ext = build_extractor_for_engine("http", PdfConfig())
    assert isinstance(ext, HttpEngine)


def test_build_extractor_for_engine_unknown_raises() -> None:
    with pytest.raises(ValueError, match=r"unknown pdf engine"):
        build_extractor_for_engine("magic", PdfConfig())


def test_build_extractor_for_engine_empty_raises() -> None:
    """Empty name in the explicit path falls through to unknown-engine error."""
    with pytest.raises(ValueError, match=r"unknown pdf engine"):
        build_extractor_for_engine("", PdfConfig())
