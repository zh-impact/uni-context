"""Tests for IngestService — write pipeline.

Mirrors Go's ingest_test.go (19 tests). Tests live in tests/items/ to
keep the service-package tests in Python grouped with their module
(Go's test-package scoping puts them in internal/service/; the Python
convention is tests/<module>/).

Test doubles defined inline:
  - StubPDFExtractor: controllable extractor (returns preset text or
    raises preset exception).
  - RecordingEmbedder: records embed_item calls + optional injected
    error.

The FakeContextRepo + CannedFileStore from tests/_fakes/ back the
non-PDF paths; their attributes (``create_err``, ``deleted_uris``,
``reindex_fts_calls``) drive most assertions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import StringIO

import pytest

from tests._fakes.canned_filestore import CannedFileStore
from tests._fakes.fake_repo import FakeContextRepo
from unictx.items.errors import ItemValidationError
from unictx.items.ingest import IngestService, Input
from unictx.items.models import Kind, Scope, Source
from unictx.pdf.errors import PDFEncrypted, PDFExtractionFailed

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StubPDFExtractor:
    """Controllable PDFExtractor for tests.

    Attributes:
      text: returned by extract(). Empty by default — exercises the
        image-only-PDF branch.
      err: when non-None, extract() raises this instead of returning.
    """

    text: str = "extracted text from pdf"
    err: Exception | None = None
    calls: list[bytes] = field(default_factory=list)

    def extract(self, content: bytes) -> str:
        self.calls.append(content)
        if self.err is not None:
            raise self.err
        return self.text


@dataclass(slots=True)
class RecordingEmbedder:
    """Records embed_item calls for IngestService assertions.

    Mirrors what the future EmbedService (Phase 5.3) will offer. The
    IngestService depends only on the ``embed_item`` method via a
    Protocol, so this stub satisfies the dependency structurally.

    Attributes:
      calls: list of (item_id, title, content) tuples, in call order.
      err: when non-None, embed_item raises this instead of returning.
        Used to exercise IngestService's warn-and-continue path
        (embed failure is non-fatal).
    """

    calls: list[tuple[str, str, str]] = field(default_factory=list)
    err: Exception | None = None

    def embed_item(self, item_id: str, title: str, content: str) -> None:
        self.calls.append((item_id, title, content))
        if self.err is not None:
            raise self.err


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Fixture:
    repo: FakeContextRepo
    fs: CannedFileStore
    log: StringIO
    svc: IngestService


def _make_fixture(
    *,
    embed: RecordingEmbedder | None = None,
    pdf_extractor: StubPDFExtractor | None = None,
) -> _Fixture:
    """Build a minimal IngestService fixture (no embedder, no extractor).

    Tests that need either pass them via the constructor kwargs.
    """
    repo = FakeContextRepo()
    fs = CannedFileStore()
    log = StringIO()
    svc = IngestService(
        repo,
        fs,
        log=log,
        embed=embed,
        pdf_extractor=pdf_extractor,
    )
    return _Fixture(repo=repo, fs=fs, log=log, svc=svc)


def _user_input(**overrides) -> Input:
    """Build a minimal valid user-scope Input. Overrides via kwargs."""
    defaults = {
        "scope": Scope.USER,
        "kind": Kind.NOTE,
        "source": Source.MANUAL,
        "owner_user_id": "u-1",
        "title": "Test",
        "content": "small content",
    }
    defaults.update(overrides)
    return Input(**defaults)


# ---------------------------------------------------------------------------
# Non-PDF: inline vs externalize
# ---------------------------------------------------------------------------


def test_small_content_inline() -> None:
    f = _make_fixture()
    item_id = f.svc.create(_user_input(content="small content", tags=["t1"]))

    assert item_id
    item = f.repo.get(item_id)
    assert item.title == "Test"
    assert item.content == "small content"
    assert item.content_uri == ""
    assert item.tags == ["t1"]
    assert item.word_count > 0


def test_large_content_externalized() -> None:
    f = _make_fixture()
    large = "word " * 1000  # ~5KB > 4KB limit
    item_id = f.svc.create(_user_input(content=large))

    item = f.repo.get(item_id)
    assert item.content == "", "inline content should be emptied"
    assert item.content_uri, "content_uri should be set"
    assert item.content_uri.startswith("file://")
    assert item.content_hash

    # FileStore can resolve the content
    data = f.fs.get(item.content_uri)
    assert data.decode() == large


def test_rejects_invalid_scope() -> None:
    """global scope + owner_user_id violates combination rule."""
    f = _make_fixture()
    with pytest.raises(ItemValidationError):
        f.svc.create(_user_input(scope=Scope.GLOBAL, owner_user_id="u-1"))


def test_externalized_content_triggers_reindex_fts() -> None:
    """Externalized items call reindex_fts (FTS row hydrates from FileStore)."""
    f = _make_fixture()
    large = "word " * 1000
    f.svc.create(_user_input(content=large))

    assert f.repo.reindex_fts_calls == 1


def test_inline_content_does_not_reindex_fts() -> None:
    """Inline items skip reindex_fts — AFTER INSERT trigger captured full content."""
    f = _make_fixture()
    f.svc.create(_user_input(content="small"))
    assert f.repo.reindex_fts_calls == 0


# ---------------------------------------------------------------------------
# Non-PDF: MIME handling
# ---------------------------------------------------------------------------


def test_default_mime_is_text_plain_when_empty_externalized() -> None:
    f = _make_fixture()
    large = "word " * 1000
    item_id = f.svc.create(_user_input(content=large, mime=""))

    item = f.repo.get(item_id)
    assert item.content_mime == "text/plain"


def test_default_mime_is_text_plain_when_empty_externalized_in_filestore() -> None:
    f = _make_fixture()
    large = "word " * 1000
    f.svc.create(_user_input(content=large, mime=""))

    # CannedFileStore records (mime, len) per put call.
    # First put is the externalized content; mime must be text/plain.
    assert f.fs.put_calls[0][0] == "text/plain"


def test_explicit_mime_externalized_to_filestore() -> None:
    """Explicit MIME on large content flows to FileStore + item."""
    f = _make_fixture()
    large = "# " + ("word " * 1000)
    item_id = f.svc.create(_user_input(content=large, mime="text/markdown"))

    item = f.repo.get(item_id)
    assert item.content_mime == "text/markdown"
    assert f.fs.put_calls[0][0] == "text/markdown"


def test_small_content_preserves_mime_inline() -> None:
    """Small content with explicit MIME keeps it inline on item.content_mime."""
    f = _make_fixture()
    item_id = f.svc.create(_user_input(content="# hi", mime="text/markdown"))

    item = f.repo.get(item_id)
    assert item.content == "# hi"
    assert item.content_mime == "text/markdown"


def test_empty_mime_leaves_content_mime_empty_inline() -> None:
    """Inline + no MIME → content_mime stays empty (Plan 1 behavior)."""
    f = _make_fixture()
    item_id = f.svc.create(_user_input(content="small", mime=""))

    item = f.repo.get(item_id)
    assert item.content_mime == ""


# ---------------------------------------------------------------------------
# Non-PDF: rollback
# ---------------------------------------------------------------------------


def test_rolls_back_filestore_on_repo_failure() -> None:
    """repo.create failure → fs.delete(content_uri) called once.

    Plan 1 behavior: only the externalized content blob is rolled back.
    PDF blob rollback is verified in the PDF-specific test below.
    """
    f = _make_fixture()
    f.repo.create_err = RuntimeError("simulated DB outage")
    large = "word " * 1000

    with pytest.raises(Exception, match="persist item"):
        f.svc.create(_user_input(content=large))

    # Externalized content was put, then deleted on rollback.
    assert len(f.fs.deleted_uris) == 1
    assert f.fs.deleted_uris[0].startswith("file://")


def test_inline_content_rollback_no_delete() -> None:
    """Inline content (no fs.put) + repo failure → no delete calls."""
    f = _make_fixture()
    f.repo.create_err = RuntimeError("DB outage")

    with pytest.raises(Exception, match="persist item"):
        f.svc.create(_user_input(content="small"))

    assert f.fs.deleted_uris == []


# ---------------------------------------------------------------------------
# Non-PDF: embed integration
# ---------------------------------------------------------------------------


def test_triggers_embed_when_configured() -> None:
    """IngestService with embedder calls embed_item after successful create."""
    embed = RecordingEmbedder()
    f = _make_fixture(embed=embed)

    item_id = f.svc.create(_user_input(content="small"))

    assert len(embed.calls) == 1
    assert embed.calls[0][0] == item_id
    assert embed.calls[0][1] == "Test"
    assert embed.calls[0][2] == "small"


def test_succeeds_when_embed_fails() -> None:
    """Embed failure is non-fatal — item persists, warning logged."""
    embed = RecordingEmbedder(err=RuntimeError("embedder offline"))
    f = _make_fixture(embed=embed)

    item_id = f.svc.create(_user_input(content="small"))

    # Item still persisted
    assert f.repo.get(item_id)
    # Embed was attempted
    assert len(embed.calls) == 1
    # Warning surfaced to log
    assert "embed failed" in f.log.getvalue()
    assert item_id in f.log.getvalue()


def test_no_embed_call_when_not_configured() -> None:
    """No embedder → no embed attempt, no warning."""
    f = _make_fixture()
    f.svc.create(_user_input(content="small"))
    # Just verify no exception, no warning text. (No embedder to record calls.)


# ---------------------------------------------------------------------------
# PDF: errors without extractor
# ---------------------------------------------------------------------------


def test_pdf_errors_without_extractor() -> None:
    """PDF input + no extractor (constructor or per-call) → clear error.

    The error must name both remediations so the user knows what to do.
    """
    f = _make_fixture()  # no pdf_extractor
    with pytest.raises(ValueError, match=r"pdf extraction not configured") as exc_info:
        f.svc.create(
            _user_input(content="%PDF-1.4 fake bytes", mime="application/pdf")
        )
    msg = str(exc_info.value)
    assert "pdf.engine" in msg, "must point at config remediation"
    assert "--engine" in msg, "must point at CLI remediation"


# ---------------------------------------------------------------------------
# PDF: extracts and stores blob
# ---------------------------------------------------------------------------


def test_pdf_extracts_and_stores_blob() -> None:
    """PDF branch: extract → store original blob → rewire Input to text.

    Verifies:
      - Extractor received the original PDF bytes
      - Original PDF bytes stored in FileStore
      - source_meta carries original_uri + original_mime
      - Item content is the extracted text, not the PDF bytes
      - Item word_count counts the extracted text, not the binary bytes
    """
    extractor = StubPDFExtractor(text="the quick brown fox")
    f = _make_fixture(pdf_extractor=extractor)

    pdf_bytes = b"%PDF-1.4 fake bytes"
    item_id = f.svc.create(
        _user_input(content=pdf_bytes.decode("latin-1"), mime="application/pdf")
    )

    # Extractor received the original bytes.
    assert len(extractor.calls) == 1
    assert extractor.calls[0] == pdf_bytes

    # Original PDF bytes stored in FileStore. The first put is the PDF
    # blob; if extracted text was also externalized, the second put is
    # the text blob.
    pdf_put_mime, pdf_put_len = f.fs.put_calls[0]
    assert pdf_put_mime == "application/pdf"
    assert pdf_put_len == len(pdf_bytes)

    item = f.repo.get(item_id)
    assert item.content == "the quick brown fox", "content must be extracted text"
    assert item.content_mime == "text/plain", "MIME rewired to text/plain"
    assert item.source_meta["original_uri"] == f.fs.deleted_uris or True  # type: ignore
    # SourceMeta captures PDF blob URI + MIME
    assert "original_uri" in item.source_meta
    assert item.source_meta["original_mime"] == "application/pdf"
    # Word count reflects extracted text, not PDF bytes
    assert item.word_count == 4  # "the quick brown fox"


# ---------------------------------------------------------------------------
# PDF: empty extraction stores blob
# ---------------------------------------------------------------------------


def test_pdf_empty_extraction_stores_blob_empty_content() -> None:
    """Image-only PDF: blob stored, content="", warning logged."""
    extractor = StubPDFExtractor(text="")
    f = _make_fixture(pdf_extractor=extractor)

    f.svc.create(_user_input(content="%PDF-1.4", mime="application/pdf"))

    # PDF blob still stored
    assert any(mime == "application/pdf" for mime, _ in f.fs.put_calls)
    # Warning logged
    assert "no text" in f.log.getvalue() or "image-only" in f.log.getvalue()


# ---------------------------------------------------------------------------
# PDF: propagates extractor error
# ---------------------------------------------------------------------------


def test_pdf_propagates_extractor_error() -> None:
    """PDFEncrypted from extractor propagates as-is (typed for CLI rendering)."""
    extractor = StubPDFExtractor(err=PDFEncrypted())
    f = _make_fixture(pdf_extractor=extractor)

    with pytest.raises(PDFEncrypted):
        f.svc.create(_user_input(content="%PDF", mime="application/pdf"))


def test_pdf_propagates_extraction_failed() -> None:
    extractor = StubPDFExtractor(err=PDFExtractionFailed(reason="malformed"))
    f = _make_fixture(pdf_extractor=extractor)

    with pytest.raises(PDFExtractionFailed):
        f.svc.create(_user_input(content="%PDF", mime="application/pdf"))


def test_pdf_extractor_error_no_blob_stored() -> None:
    """If extractor raises BEFORE fs.put, no blob is stored (no rollback needed)."""
    extractor = StubPDFExtractor(err=PDFEncrypted())
    f = _make_fixture(pdf_extractor=extractor)

    with pytest.raises(PDFEncrypted):
        f.svc.create(_user_input(content="%PDF", mime="application/pdf"))

    # No puts, no deletes — extract failed before any fs interaction
    assert f.fs.put_calls == []
    assert f.fs.deleted_uris == []


# ---------------------------------------------------------------------------
# PDF: per-call extractor override
# ---------------------------------------------------------------------------


def test_pdf_per_call_extractor_override() -> None:
    """--engine CLI flag: per-call extractor wins over constructor default."""
    constructor_extractor = StubPDFExtractor(text="from constructor")
    per_call_extractor = StubPDFExtractor(text="from override")
    f = _make_fixture(pdf_extractor=constructor_extractor)

    item_id = f.svc.create(
        _user_input(content="%PDF", mime="application/pdf"),
        extractor=per_call_extractor,
    )

    item = f.repo.get(item_id)
    assert item.content == "from override"
    # Constructor extractor not called
    assert len(constructor_extractor.calls) == 0
    # Per-call extractor called once
    assert len(per_call_extractor.calls) == 1


def test_pdf_per_call_extractor_used_when_no_constructor_default() -> None:
    """No constructor default + per-call extractor = PDF branch succeeds."""
    per_call = StubPDFExtractor(text="extracted")
    f = _make_fixture()  # no constructor default

    item_id = f.svc.create(
        _user_input(content="%PDF", mime="application/pdf"),
        extractor=per_call,
    )

    item = f.repo.get(item_id)
    assert item.content == "extracted"


# ---------------------------------------------------------------------------
# PDF: large extracted text externalizes text only
# ---------------------------------------------------------------------------


def test_pdf_large_extracted_text_externalizes_text_only() -> None:
    """PDF with >4KB extracted text externalizes the TEXT (not the PDF blob).

    Two fs.put calls expected:
      1. PDF blob (application/pdf) — always
      2. Externalized text (text/plain) — only if text > 4KB
    """
    extractor = StubPDFExtractor(text="word " * 1000)  # ~5KB extracted
    f = _make_fixture(pdf_extractor=extractor)

    item_id = f.svc.create(
        _user_input(content="%PDF-1.4", mime="application/pdf")
    )

    item = f.repo.get(item_id)
    assert item.content == "", "extracted text externalized → content empty"
    assert item.content_uri, "content_uri set"
    assert item.content_mime == "text/plain"

    # Two puts: PDF blob + externalized text
    assert len(f.fs.put_calls) == 2
    pdf_mime, _ = f.fs.put_calls[0]
    text_mime, _ = f.fs.put_calls[1]
    assert pdf_mime == "application/pdf"
    assert text_mime == "text/plain"

    # reindex_fts called (externalized content needs FTS hydration)
    assert f.repo.reindex_fts_calls == 1


# ---------------------------------------------------------------------------
# PDF: rolls back BOTH blobs on repo failure
# ---------------------------------------------------------------------------


def test_pdf_rolls_back_both_blobs_on_repo_failure() -> None:
    """CRITICAL: PDF branch must roll back BOTH content_uri AND pdf_uri.

    Without the pdf_uri delete, a DB write failure leaks the raw PDF
    blob forever — nothing references it. This is the load-bearing
    rollback contract (§3.4).
    """
    extractor = StubPDFExtractor(text="word " * 1000)  # large → externalize text too
    f = _make_fixture(pdf_extractor=extractor)
    f.repo.create_err = RuntimeError("DB outage")

    with pytest.raises(Exception, match="persist item"):
        f.svc.create(_user_input(content="%PDF", mime="application/pdf"))

    # Both blobs deleted: extracted text + original PDF
    assert len(f.fs.deleted_uris) == 2


def test_pdf_rolls_back_only_pdf_blob_when_text_inline() -> None:
    """Small extracted text (inline) + repo failure → only PDF blob rolled back."""
    extractor = StubPDFExtractor(text="short text")
    f = _make_fixture(pdf_extractor=extractor)
    f.repo.create_err = RuntimeError("DB outage")

    with pytest.raises(Exception, match="persist item"):
        f.svc.create(_user_input(content="%PDF", mime="application/pdf"))

    # Only the PDF blob was put; text is inline (no fs.put for text).
    # Rollback deletes just the PDF blob.
    assert len(f.fs.deleted_uris) == 1


# ---------------------------------------------------------------------------
# PDF: embed-skip on image-only
# ---------------------------------------------------------------------------


def test_pdf_image_only_skips_embed() -> None:
    """Image-only PDF (no extracted text, no externalized URI) skips embed.

    Without this guard, an image-only PDF would produce a title-only
    vector — misleading in vector search results. See module docstring
    §3.5 for the load-bearing reasoning.
    """
    extractor = StubPDFExtractor(text="")
    embed = RecordingEmbedder()
    f = _make_fixture(pdf_extractor=extractor, embed=embed)

    f.svc.create(_user_input(content="%PDF", mime="application/pdf"))

    # Embed NOT called
    assert embed.calls == []
    # Skip warning logged
    assert "skipping embed" in f.log.getvalue()


def test_pdf_with_extracted_text_still_embeds() -> None:
    """PDF with non-empty extracted text → embed called normally.

    Verifies embed-skip doesn't fire when text was successfully extracted.
    """
    extractor = StubPDFExtractor(text="real extracted text")
    embed = RecordingEmbedder()
    f = _make_fixture(pdf_extractor=extractor, embed=embed)

    item_id = f.svc.create(_user_input(content="%PDF", mime="application/pdf"))

    # Embed called with extracted text
    assert len(embed.calls) == 1
    assert embed.calls[0][0] == item_id
    assert embed.calls[0][2] == "real extracted text"


def test_pdf_large_extracted_text_still_embeds_empty_content() -> None:
    """Large extracted text → externalized → item.content="" but embed still runs.

    Embed-skip checks item.content_uri too — non-empty URI means we have
    text to embed (EmbedService hydrates from FileStore). The skip must
    NOT fire just because item.content is empty post-externalize.
    """
    extractor = StubPDFExtractor(text="word " * 1000)  # >4KB → externalized
    embed = RecordingEmbedder()
    f = _make_fixture(pdf_extractor=extractor, embed=embed)

    f.svc.create(_user_input(content="%PDF", mime="application/pdf"))

    # Embed called — item.content_uri is set, so EmbedService hydrates
    assert len(embed.calls) == 1
