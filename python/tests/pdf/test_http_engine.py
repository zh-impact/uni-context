"""Tests for HttpEngine — httpx-backed PDF extractor.

Uses httpx.MockTransport so tests run hermetically without a live HTTP
server. Mirrors the test layout of Go's http_test.go (5 cases) plus
Python-side defaults/charset coverage.

Cases:
  - Protocol conformance
  - Default timeout (30s, zero/negative falls back)
  - POST binary with auth header → returns response body
  - Auth header omitted when token empty
  - Non-2xx → error includes status code + body snippet (≤256 chars)
  - Wrong response Content-Type → error mentions expected MIME
  - Timeout → error mentions timeout
"""

from __future__ import annotations

import httpx
import pytest

from unictx.pdf.errors import PDFExtractionFailed
from unictx.pdf.extractor import PDFExtractor
from unictx.pdf.http_engine import HttpEngine


def _engine_with_mock(
    handler,
    *,
    url: str = "http://test/extract",
    auth_token: str = "",
    timeout: float = 5.0,
) -> HttpEngine:
    """Build an HttpEngine whose httpx.Client uses a MockTransport.

    The handler is a callable matching httpx.MockTransport's signature:
    ``handler(request: httpx.Request) -> httpx.Response``.
    """
    engine = HttpEngine(url=url, timeout=timeout, auth_token=auth_token)
    engine._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=timeout,
    )
    return engine


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_http_engine_satisfies_protocol() -> None:
    assert isinstance(HttpEngine(url="http://example.com"), PDFExtractor)


# ---------------------------------------------------------------------------
# Default timeout
# ---------------------------------------------------------------------------


def test_default_timeout_is_30s() -> None:
    assert HttpEngine(url="http://x")._timeout == 30.0


def test_zero_timeout_falls_back_to_default() -> None:
    """Go: `if timeout <= 0 { timeout = 30s }` — zero/negative = default."""
    assert HttpEngine(url="http://x", timeout=0)._timeout == 30.0
    assert HttpEngine(url="http://x", timeout=-1)._timeout == 30.0


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_post_binary_returns_text_body() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["content_type"] = request.headers.get("Content-Type")
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = request.content
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            content=b"server extracted this text",
        )

    engine = _engine_with_mock(handler, auth_token="tok-abc")
    text = engine.extract(b"%PDF-1.4 fake bytes")

    assert text == "server extracted this text"
    assert captured["path"] == "/extract"
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/pdf"
    assert captured["auth"] == "Bearer tok-abc"
    assert captured["body"] == b"%PDF-1.4 fake bytes"


def test_extract_returns_str_not_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/plain"},
            content=b"hi",
        )

    assert isinstance(_engine_with_mock(handler).extract(b""), str)


def test_empty_response_body_returns_empty_string() -> None:
    """Empty extraction is NOT an error — matches FitzEngine contract."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/plain"},
            content=b"",
        )

    assert _engine_with_mock(handler).extract(b"") == ""


# ---------------------------------------------------------------------------
# Auth header
# ---------------------------------------------------------------------------


def test_auth_header_omitted_when_token_empty() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/plain"},
            content=b"",
        )

    _engine_with_mock(handler, auth_token="").extract(b"")
    assert captured["auth"] is None, "no Authorization header when token empty"


def test_auth_header_present_when_token_set() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/plain"},
            content=b"",
        )

    _engine_with_mock(handler, auth_token="sk-test").extract(b"")
    assert captured["auth"] == "Bearer sk-test"


# ---------------------------------------------------------------------------
# Non-2xx status
# ---------------------------------------------------------------------------


def test_non_2xx_raises_extraction_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=422,
            content=b"malformed pdf body",
        )

    with pytest.raises(PDFExtractionFailed) as exc_info:
        _engine_with_mock(handler).extract(b"fake")
    msg = str(exc_info.value)
    assert "422" in msg
    assert "malformed pdf body" in msg


def test_non_2xx_body_snippet_truncated() -> None:
    """Body snippet beyond 256 chars is dropped (matches Go's cap)."""
    long_body = b"x" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=500, content=long_body)

    with pytest.raises(PDFExtractionFailed) as exc_info:
        _engine_with_mock(handler).extract(b"")
    x_count = str(exc_info.value).count("x")
    assert x_count < 1000, f"body not truncated: got {x_count} x chars"


# ---------------------------------------------------------------------------
# Wrong response Content-Type
# ---------------------------------------------------------------------------


def test_wrong_response_mime_raises_extraction_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "application/json"},
            content=b'{"text": "hi"}',
        )

    with pytest.raises(PDFExtractionFailed) as exc_info:
        _engine_with_mock(handler).extract(b"fake")
    assert "text/plain" in str(exc_info.value), "error must mention expected MIME"


def test_charset_suffix_accepted() -> None:
    """Content-Type 'text/plain; charset=utf-8' should be accepted."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            content=b"hi",
        )

    assert _engine_with_mock(handler).extract(b"") == "hi"


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_timeout_raises_extraction_failed() -> None:
    """MockTransport handler raises ReadTimeout → engine surfaces timeout."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout", request=request)

    with pytest.raises(PDFExtractionFailed) as exc_info:
        _engine_with_mock(handler, timeout=0.1).extract(b"fake")
    assert "timeout" in str(exc_info.value).lower()
