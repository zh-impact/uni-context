"""PDF extractor factory — picks the right engine from config.

Behavior-port of Go's `internal/app/pdf.go`. Returns the right
:class:`PDFExtractor` based on the configured engine name, or ``None``
when PDF is unconfigured.

Engine names:
  - ``""`` (empty) → returns ``None``; the caller proceeds without PDF
    support. IngestService raises a clear error if a PDF is passed in.
  - ``"fitz"`` → :class:`FitzEngine` (PyMuPDF; the default in Python).
    Equivalent to Go's ``gxpdf``.
  - ``"shell"`` → :class:`ShellEngine` wrapping an external binary
    (e.g. pdftotext). Requires ``pdf.engines.shell.command``.
  - ``"http"`` → :class:`HttpEngine` POSTing to a service. Requires
    ``pdf.engines.http.url``.

Misconfiguration raises :class:`ValueError` with a message naming the
specific config key the user must set (matches Go's error text).

The Go factory takes an ``io.Writer`` log param; the Python engines
don't log inline (they raise structured exceptions instead), so the
log param is dropped. This matches the established pattern in the
embed/storage modules.
"""

from __future__ import annotations

from unictx.config import PdfConfig, PdfEnginesConfig
from unictx.pdf.extractor import PDFExtractor
from unictx.pdf.fitz_engine import FitzEngine
from unictx.pdf.http_engine import HttpEngine
from unictx.pdf.shell_engine import ShellEngine

__all__ = ["build_pdf_extractor", "build_extractor_for_engine"]


def build_pdf_extractor(cfg: PdfConfig) -> PDFExtractor | None:
    """Return the configured extractor, or None when PDF is unconfigured.

    Mirrors Go's ``BuildPDFExtractor``. Callers MUST handle ``None``
    gracefully — IngestService raises a clear error if a PDF arrives
    without an extractor configured.
    """
    if cfg.engine == "":
        return None
    return _build_extractor(cfg.engine, cfg.engines)


def build_extractor_for_engine(name: str, cfg: PdfConfig) -> PDFExtractor:
    """Return an extractor for an explicit engine name.

    Used by the CLI when ``--engine`` overrides the config default.
    Empty ``name`` is invalid here (falls through to the unknown-engine
    error) — use :func:`build_pdf_extractor` for config-driven
    selection where empty means "disabled".
    """
    return _build_extractor(name, cfg.engines)


def _build_extractor(name: str, engines: PdfEnginesConfig) -> PDFExtractor:
    if name == "fitz":
        # FitzEngine is stateless — no config to forward.
        return FitzEngine()
    if name == "shell":
        # Default ShellPdfEngineConfig.command is "pdftotext - -", so
        # this check fires only when the user explicitly sets the
        # command to "" in YAML. Matches Go's `ec.Command == ""` guard.
        if not engines.shell.command:
            raise ValueError(
                'engine "shell" not configured '
                "(set pdf.engines.shell.command in config.yaml)"
            )
        return ShellEngine(
            command=engines.shell.command,
            timeout=float(engines.shell.timeout_seconds),
        )
    if name == "http":
        if not engines.http.url:
            raise ValueError(
                'engine "http" not configured '
                "(set pdf.engines.http.url in config.yaml)"
            )
        return HttpEngine(
            url=engines.http.url,
            timeout=float(engines.http.timeout_seconds),
            auth_token=engines.http.auth_token,
        )
    raise ValueError(
        f'unknown pdf engine "{name}" (want fitz|shell|http)'
    )
