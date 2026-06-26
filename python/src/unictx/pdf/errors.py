"""pdf module — PDF extraction errors.

All inherit from unictx.errors.UnictxError so CLI can catch them
collectively via `except UnictxError`. The three error classes map to
the three distinct failure modes PDFExtractor can encounter — they
deserve separate types so the CLI can render user-facing advice
differently (encrypted vs. command-not-found vs. generic failure).
"""

from unictx.errors import UnictxError


class PDFEncrypted(UnictxError):
    """Raised by PDFExtractor.extract when the PDF requires a password.

    User-facing advice: the PDF cannot be read without credentials.
    Re-save the PDF without encryption, or supply the password
    out-of-band (not currently supported by uni-context).
    """

    def __init__(self, reason: str = "pdf is encrypted"):
        super().__init__(reason)
        self.reason = reason


class PDFExtractionFailed(UnictxError):
    """Raised by PDFExtractor.extract on generic extraction failure.

    Covers malformed PDF, IO error, downstream HTTP 5xx. The wrapped
    reason carries the underlying error text for diagnostics.
    """

    def __init__(self, reason: str):
        super().__init__(f"pdf extraction failed: {reason}")
        self.reason = reason


class PDFCommandNotFound(UnictxError):
    """Raised by shell-based PDF engines when the binary is missing.

    Covers pdftotext, mutool, etc. not on PATH. User-facing advice:
    install the missing tool (e.g. `brew install poppler` for
    pdftotext) or switch to an HTTP-backed engine in config.
    """

    def __init__(self, command: str):
        super().__init__(f"pdf command not found: {command}")
        self.command = command
