"""HTTP-based PDF extractor — POSTs bytes to an extraction service.

Behavior-port of Go's `internal/adapter/pdf/http.go`. The engine is
deployment-flexible: any service that accepts ``application/pdf`` on the
request body and returns the extracted text as ``text/plain`` will do
(e.g. a hosted OCR service, a self-hosted pdftotext wrapper, etc.).

Error mapping matches Go:
  - Transport error w/ timeout → PDFExtractionFailed("http request
    timeout after Ns")
  - Transport error (other) → PDFExtractionFailed("http request: ...")
  - Non-2xx → PDFExtractionFailed(f"http {code}: {body snippet}")
    where the body snippet is capped at 256 bytes (matches Go's
    ``io.LimitReader(resp.Body, 256)``)
  - Wrong response Content-Type → PDFExtractionFailed(
    f'unexpected response MIME "{ct}", want text/plain')

The URL is used as-is — no path appending. Callers configure the full
URL including any path (``http://host:port/extract``).

No ctx param: Python is sync. Go's ctx forwards to
http.NewRequestWithContext for cancellation; httpx's per-request
timeout covers the bounded-runtime case.
"""

from __future__ import annotations

import httpx

from unictx.pdf.errors import PDFExtractionFailed
from unictx.pdf.extractor import PDFExtractor

__all__ = ["HttpEngine"]

_DEFAULT_TIMEOUT = 30.0
_ERROR_BODY_SNIPPET = 256


class HttpEngine:
    """HTTP-backed PDFExtractor — POSTs bytes to an extraction service.

    Owns a long-lived :class:`httpx.Client`. Tests may replace
    ``self._client`` with one wired to :class:`httpx.MockTransport`.

    Construct with the full URL (including path), an optional timeout
    in seconds (defaults to 30; zero/negative falls back to default,
    matching Go), and an optional bearer token. An empty token omits
    the ``Authorization`` header entirely.
    """

    def __init__(
        self,
        url: str,
        timeout: float | None = None,
        auth_token: str = "",
    ) -> None:
        self._url = url
        # Go: `if timeout <= 0 { timeout = 30s }` — zero/negative = default.
        if timeout and timeout > 0:
            self._timeout = timeout
        else:
            self._timeout = _DEFAULT_TIMEOUT
        self._auth_token = auth_token
        self._client: httpx.Client = httpx.Client(timeout=self._timeout)

    def extract(self, content: bytes) -> str:
        """POST ``content`` to the URL, return the response body as text.

        Returns:
            Decoded response body as str. May be empty — empty
            extraction is NOT an error, matching FitzEngine's contract.

        Raises:
            PDFExtractionFailed: Any HTTP failure — timeout, non-2xx,
                wrong response Content-Type, transport error.
        """
        headers = {"Content-Type": "application/pdf"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        try:
            response = self._client.post(
                self._url, content=content, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise PDFExtractionFailed(
                f"http request timeout after {self._timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            # Covers InvalidURL, ProtocolError, ConnectError, etc.
            raise PDFExtractionFailed(f"http request: {exc}") from exc

        if not (200 <= response.status_code < 300):
            # Body snippet bounded to 256 bytes: error responses can be
            # arbitrarily large HTML/JSON, and we only need enough to
            # surface the failure reason. Byte-faithful to Go's
            # `io.LimitReader(resp.Body, 256)` — slice first, then
            # decode with errors='replace' so multi-byte chars cut at
            # the cap degrade gracefully rather than crash.
            body_text = (
                response.content[:_ERROR_BODY_SNIPPET]
                .decode("utf-8", errors="replace")
                .strip()
            )
            raise PDFExtractionFailed(
                f"http {response.status_code}: {body_text}"
            )

        ct = response.headers.get("Content-Type", "")
        if not ct.lower().startswith("text/plain"):
            raise PDFExtractionFailed(
                f'unexpected response MIME "{ct}", want text/plain'
            )

        return response.text


# Compile-time-ish Protocol check (catches accidental signature drift
# at import time).
_IS_PROTOCOL: PDFExtractor = HttpEngine(url="http://example.com")  # type: ignore[assignment]
del _IS_PROTOCOL
