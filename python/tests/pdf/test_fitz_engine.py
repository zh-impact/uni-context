"""Tests for FitzEngine — PyMuPDF-backed PDF extractor.

Real PDF fixtures copied from
`archive/go/internal/adapter/pdf/testdata/` so tests exercise actual
pymupdf behavior rather than mock-driven byte wrangling.

Fixture inventory (inspected at test-author time):
  - sample.pdf:    1 page, content "the quick brown fox ..."
  - blank.pdf:     1 page, content "" (image-only or truly empty)
  - encrypted.pdf: 1 page, needs_pass=True
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unictx.pdf.errors import PDFEncrypted, PDFExtractionFailed
from unictx.pdf.extractor import PDFExtractor
from unictx.pdf.fitz_engine import FitzEngine

_TESTDATA = Path(__file__).parent / "testdata"


def _fixture(name: str) -> bytes:
    return (_TESTDATA / name).read_bytes()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_fitz_engine_satisfies_protocol() -> None:
    """FitzEngine must satisfy the PDFExtractor Protocol."""
    assert isinstance(FitzEngine(), PDFExtractor)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_extract_sample_returns_page_text() -> None:
    """sample.pdf has one page with the canonical pangram."""
    text = FitzEngine().extract(_fixture("sample.pdf"))
    assert "the quick brown fox" in text
    assert "lazy dog" in text


def test_extract_returns_str_not_bytes() -> None:
    """extract() returns str — pymupdf's get_text already decodes."""
    text = FitzEngine().extract(_fixture("sample.pdf"))
    assert isinstance(text, str)


def test_extract_blank_pdf_returns_empty_string_not_error() -> None:
    """blank.pdf returns "" — empty extraction is NOT an error.

    The Protocol contract requires image-only/blank PDFs to surface as
    empty text so callers can decide UX (store with empty content,
    prompt user to OCR, etc.).
    """
    text = FitzEngine().extract(_fixture("blank.pdf"))
    assert text == ""


def test_extract_blank_pdf_does_not_raise() -> None:
    """Sanity check: explicit no-exception assertion on blank.pdf."""
    engine = FitzEngine()
    # Should not raise.
    engine.extract(_fixture("blank.pdf"))


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


def test_extract_encrypted_raises_pdf_encrypted() -> None:
    """encrypted.pdf requires a password; raises PDFEncrypted."""
    with pytest.raises(PDFEncrypted):
        FitzEngine().extract(_fixture("encrypted.pdf"))


def test_pdf_encrypted_carries_reason_attribute() -> None:
    """PDFEncrypted carries a `reason` for CLI rendering."""
    with pytest.raises(PDFEncrypted) as exc_info:
        FitzEngine().extract(_fixture("encrypted.pdf"))
    assert exc_info.value.reason


def test_pdf_encrypted_message_contains_encrypt() -> None:
    """Brief requires 'encrypted pdf' or 'encrypt' substring in message.

    CLI error renderers depend on this substring to differentiate from
    other UnictxError subclasses.
    """
    with pytest.raises(PDFEncrypted) as exc_info:
        FitzEngine().extract(_fixture("encrypted.pdf"))
    assert "encrypt" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


def test_extract_malformed_bytes_raises_extraction_failed() -> None:
    """Random bytes aren't a valid PDF; raises PDFExtractionFailed."""
    with pytest.raises(PDFExtractionFailed):
        FitzEngine().extract(b"not a pdf at all")


def test_extract_empty_bytes_raises_extraction_failed() -> None:
    """Empty bytes can't be opened as a PDF."""
    with pytest.raises(PDFExtractionFailed):
        FitzEngine().extract(b"")


def test_pdf_extraction_failed_carries_reason() -> None:
    """PDFExtractionFailed carries a `reason` for diagnostics."""
    with pytest.raises(PDFExtractionFailed) as exc_info:
        FitzEngine().extract(b"garbage")
    assert exc_info.value.reason


# ---------------------------------------------------------------------------
# Multi-page behavior
# ---------------------------------------------------------------------------


def test_extract_multipage_concatenates_with_newlines() -> None:
    """Multi-page PDFs concatenate page text with newlines.

    The fixtures only ship single-page PDFs, so build a synthetic
    multi-page PDF in-memory using pymupdf's API. This exercises the
    concatenation contract without depending on a fixture we don't ship.
    """
    import pymupdf

    doc = pymupdf.open()
    for text in ("alpha page", "beta page", "gamma page"):
        page = doc.new_page()
        page.insert_text((72, 72), text)
    buf = doc.tobytes()
    doc.close()

    extracted = FitzEngine().extract(buf)
    assert "alpha page" in extracted
    assert "beta page" in extracted
    assert "gamma page" in extracted
    # Pages appear in input order. PyMuPDF's get_text() returns a
    # trailing newline per page, so the join produces extra blank
    # lines — we assert relative ordering, not strict concatenation.
    assert extracted.index("alpha page") < extracted.index("beta page")
    assert extracted.index("beta page") < extracted.index("gamma page")
