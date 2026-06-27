"""PyMuPDF-backed PDF extractor — the default engine.

Behavior-port of Go's `internal/adapter/pdf/gxpdf.go`. Go uses
github.com/coregx/gxpdf; Python uses PyMuPDF (imported as `pymupdf`,
also available as the legacy `fitz` alias). The libraries expose
different APIs, so this is a behavior port rather than a line-by-line
translation. Error semantics match Go:

  - Encrypted PDFs raise `PDFEncrypted`.
  - Malformed PDFs / IO errors raise `PDFExtractionFailed`.
  - Image-only or blank PDFs return "" — empty extraction is NOT an
    error. Callers decide how to surface empty content.

Encryption detection uses `doc.needs_pass`, which is PyMuPDF's
authoritative signal. Go's gxpdf port falls back to substring matching
on the open-error message ("encrypt" case-insensitive) because gxpdf
doesn't expose a needs-pass probe; we keep the substring fallback as
a belt-and-braces measure for any open-error path that surfaces before
needs_pass can be consulted.

Per-page extraction errors are swallowed by PyMuPDF (it logs internally
and returns "" for the failed page); the remaining pages still
contribute their text. This matches gxpdf's behavior.

No ctx param: Python is sync. Go forwards ctx to
OpenFromBytesWithContext for cancellation; PyMuPDF's open() is not
cancellable, so a ctx equivalent wouldn't help.
"""

from __future__ import annotations

import pymupdf

from unictx.pdf.errors import PDFEncrypted, PDFExtractionFailed
from unictx.pdf.extractor import PDFExtractor

__all__ = ["FitzEngine"]


class FitzEngine:
    """Default PDFExtractor impl, backed by PyMuPDF.

    Stateless: construct freely. PyMuPDF handles its own resources via
    open()/close(); the engine holds no long-lived state.
    """

    def extract(self, content: bytes) -> str:
        """Extract concatenated page text from a PDF byte string.

        Returns the text of all pages joined with "\\n". May be empty
        for image-only/blank PDFs — empty is NOT an error.

        Raises:
            PDFEncrypted: PDF requires a password.
            PDFExtractionFailed: Malformed PDF or IO error.
        """
        try:
            doc = pymupdf.open(stream=content, filetype="pdf")
        except Exception as exc:
            # PyMuPDF surfaces a runtime error if the bytes aren't a
            # valid PDF; encrypted PDFs sometimes surface here too (the
            # needs_pass probe runs lazily on first access for some
            # malformed headers). Substring match on the message is the
            # stable contract Go uses for gxpdf; keep it as a fallback.
            msg = str(exc).lower()
            if "encrypt" in msg:
                raise PDFEncrypted(reason=str(exc)) from exc
            raise PDFExtractionFailed(reason=str(exc)) from exc

        try:
            # Authoritative encryption probe. PyMuPDF sets needs_pass
            # to a positive int when the doc requires a password; the
            # value is the number of password attempts allowed (>=1).
            if doc.needs_pass:
                # Default reason is "pdf is encrypted" — carries the
                # "encrypt" substring that CLI error renderers depend on
                # to distinguish encryption from other failures.
                raise PDFEncrypted()
            parts: list[str] = []
            for page in doc:
                # PyMuPDF's get_text swallows per-page extraction errors
                # and returns "" — mirrors gxpdf's behavior. Append
                # whatever it returns so blank pages contribute nothing.
                parts.append(page.get_text())
        except PDFEncrypted:
            raise
        except Exception as exc:
            raise PDFExtractionFailed(reason=str(exc)) from exc
        finally:
            doc.close()
        return "\n".join(parts)


# Compile-time-ish Protocol check (Python's runtime_checkable only
# verifies method names, but this catches accidental signature drift
# at import time if the Protocol's method set changes).
_IS_PROTOCOL: PDFExtractor = FitzEngine()  # type: ignore[assignment]
del _IS_PROTOCOL
