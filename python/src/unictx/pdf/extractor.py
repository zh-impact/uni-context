"""PDFExtractor Protocol.

Ports Go's internal/port/pdf.go. Single-method Protocol — extraction
is the only PDF operation uni-context needs. Error semantics carry
more nuance than the method count suggests (see Extract docstring).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PDFExtractor(Protocol):
    """Extracts plain text from a PDF document. Mirrors Go's port.PDFExtractor.

    Implementations live in pdf/ (Phase 2). Concurrency safety is the
    impl's responsibility — pymupdf and shell-out subprocesses are both
    safe to call from multiple threads/coroutines with proper resource
    handling.
    """

    def extract(self, content: bytes) -> str:
        """Extract text from a PDF byte string.

        Returns:
            Extracted text. May be empty (image-only/scanned PDF with
            no text layer) — empty extraction is NOT an error. Callers
            decide how to handle empty text per their UX; the
            user-note-add flow stores the PDF blob with empty content
            in this case.

        Raises:
            PDFEncrypted: PDF requires a password to read.
            PDFExtractionFailed: Malformed PDF, IO error, downstream
                HTTP 5xx — any actual failure. Callers SHOULD surface
                these to the user.
            PDFCommandNotFound: Shell-based engine invoked but the
                binary (pdftotext, mutool, etc.) is not on PATH.
        """
        ...
