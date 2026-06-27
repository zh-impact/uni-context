"""IngestService — user-facing write pipeline (CRITICAL: invariant-dense).

Behavior-port of Go's ``internal/service/ingest.go``. The most
invariant-dense service in the codebase — three load-bearing contracts
must be preserved exactly:

  1. **PDF branch ordering (Go archive §3.3):** PDF extraction runs
     BEFORE ``new_context_item`` so (a) ``word_count`` reflects the
     EXTRACTED TEXT (binary PDF bytes would give garbage counts), and
     (b) the externalize step stores EXTRACTED TEXT in FileStore
     (binary PDF bytes would give a garbage blob).

  2. **Rollback contract (§3.4):** on ``repo.create`` failure,
     ``fs.delete`` BOTH ``item.content_uri`` AND ``pdf_uri``
     (SourceMeta["original_uri"]). Without the second delete, a DB
     write failure leaks the raw PDF blob forever — nothing references
     it. The first delete (Plan 1 behavior) handles externalized
     extracted-text blobs.

  3. **Embed-skip scope (§3.5):** ``pdf_uri != "" and not item.content
     and not item.content_uri`` → skip embedding. Without this guard,
     an image-only PDF (no extracted text) would still produce a
     title-only vector — misleading because a downstream vector search
     would surface a hit for a document whose body the user can't
     actually read as text. The ``pdf_uri != ""`` check is load-bearing:
     we must NOT change embed behavior for non-PDF empty-content items
     (TestIngest_Create_TriggersEmbed_WhenConfigured calls embed
     unconditionally on empty content).

Adaptations vs Go (per Plan §Python Conventions):
  - ctx dropped (Python is sync; all calls block).
  - Go's functional options (``WithPDFExtractor``, ``WithExtractor``)
    become keyword args: constructor ``pdf_extractor=ext`` and per-call
    ``extractor=ext``. Per-call wins (applied after constructor default
    is seeded).
  - Go's ``NewIngestServiceWithEmbedder`` is collapsed into the same
    constructor via ``embed=svc`` kwarg.
  - Errors propagate as exceptions instead of ``(string, error)``.
    PDF errors (PDFEncrypted/PDFExtractionFailed/PDFCommandNotFound)
    propagate as-is — they're already typed and carry enough context.
  - ``log`` defaults to ``sys.stderr``; tests pass ``io.StringIO`` to
    assert on warnings.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from typing import IO, Any, Protocol

from unictx.items.models import (
    CONTENT_INLINE_LIMIT,
    Kind,
    NewItemParams,
    Scope,
    Source,
    count_words,
    new_context_item,
)
from unictx.items.repo import ContextRepo
from unictx.pdf.extractor import PDFExtractor
from unictx.storage.filestore import FileStore

__all__ = ["IngestService", "Input"]


class _Embedder(Protocol):
    """EmbedService-shaped dependency. EmbedService (Phase 5.3) satisfies
    this structurally. Decouples IngestService from the concrete
    EmbedService class — IngestService only needs the ``embed_item``
    method of the embed pipeline."""

    def embed_item(self, item_id: str, title: str, content: str) -> None: ...


@dataclass(slots=True)
class Input:
    """User-facing write request. Mirrors Go's ``service.Input`` struct.

    ``mime`` empty means "treat as text/plain" (preserves existing
    behavior for inline/stdin notes — no caller update required).
    The CLI sets it when importing a .md / .pdf file so FileStore's
    .meta and ``item.content_mime`` both carry the right MIME.
    """

    scope: Scope
    kind: Kind
    source: Source
    owner_user_id: str = ""
    project_id: str = ""
    agent_id: str = ""

    title: str = ""
    summary: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    source_meta: dict[str, Any] = field(default_factory=dict)

    mime: str = ""


class IngestService:
    """Write pipeline: validate → (PDF extract) → externalize → persist →
    (reindex FTS) → (embed).

    Construction is cheap (no I/O). ``repo`` and ``fs`` are required;
    ``embed`` and ``pdf_extractor`` are optional (None = feature
    disabled; behaves as Plan 1 — no vector writes, PDF inputs rejected
    with a clear actionable error).
    """

    def __init__(
        self,
        repo: ContextRepo,
        fs: FileStore,
        log: IO[str] | None = None,
        *,
        embed: _Embedder | None = None,
        pdf_extractor: PDFExtractor | None = None,
    ) -> None:
        self._repo = repo
        self._fs = fs
        # Default to stderr so prod warnings surface without forcing
        # every caller to pass a writer. Tests inject StringIO to
        # assert on warn-and-continue paths.
        self._log: IO[str] = log if log is not None else sys.stderr
        self._embed: _Embedder | None = embed
        self._pdf_extractor: PDFExtractor | None = pdf_extractor

    def create(
        self,
        input: Input,
        *,
        extractor: PDFExtractor | None = None,
    ) -> str:
        """Persist a new item. Returns the new item's id.

        Per-call ``extractor`` overrides the constructor's
        ``pdf_extractor`` for this invocation only. Used by the CLI's
        ``--engine`` flag to choose a different PDF engine without
        rebuilding the service.

        Raises:
            ValueError: PDF input arrived but no extractor is configured
                (constructor or per-call). The message names both
                remediations.
            PDFEncrypted / PDFExtractionFailed / PDFCommandNotFound:
                Propagated from the configured extractor.
            ItemValidationError: From new_context_item if scope/kind/
                source/owner/project/agent combination is illegal.
            Exception: Any fs.put / repo.create failure propagates
                directly (rollback is performed first when applicable).
        """
        # Per-call override wins. Go: cfg.extractor seeded from
        # s.pdfExtractor, then per-call opts run.
        active_extractor = extractor if extractor is not None else self._pdf_extractor

        # Captured at function scope so the repo.create rollback can
        # clean up the PDF blob too. Empty when no PDF branch ran.
        pdf_uri = ""

        # ---- PDF branch runs BEFORE new_context_item -------------------
        # Two reasons (Go archive §3.3):
        #   1. item.word_count = count_words(in.content) further down
        #      must count the EXTRACTED TEXT, not raw PDF bytes
        #      (otherwise the count is meaningless binary garbage).
        #   2. The externalize step (fs.put(in.content_bytes, mime))
        #      must store extracted text, not PDF bytes (otherwise
        #      FileStore blob is binary garbage and FTS hydration
        #      returns garbage).
        # Rewriting in.content / in.mime here means the rest of create
        # is PDF-unaware; it just sees a normal text Input.
        if input.mime == "application/pdf":
            if active_extractor is None:
                raise ValueError(
                    "pdf extraction not configured: "
                    "set pdf.engine in config or pass --engine"
                )
            # Defensive: the CLI initializes source_meta, but a future
            # API caller could pass None.
            if input.source_meta is None:
                input.source_meta = {}

            text = active_extractor.extract(input.content.encode("utf-8"))

            # Store the ORIGINAL PDF bytes (not the extracted text —
            # that flows through the normal externalize path below).
            # The URI is captured on source_meta so the blob is
            # retrievable later for re-extraction, download, preview.
            pdf_uri, _ = self._fs.put(
                input.content.encode("utf-8"), "application/pdf"
            )

            if text == "":
                # Image-only / scanned PDF: no text layer to extract.
                # We still persist the blob (user may want to download
                # or OCR later), but downstream embed must be skipped
                # to avoid producing a title-only vector (see
                # skip_embed below).
                self._warn(
                    "warning: pdf extraction yielded no text (likely "
                    "image-only or scanned); storing blob with empty "
                    "content — search/embedding will not hit body text\n"
                )

            input.source_meta["original_uri"] = pdf_uri
            input.source_meta["original_mime"] = "application/pdf"

            # Rewire Input so the rest of create sees plain text.
            input.content = text
            input.mime = "text/plain"

        # ---- Build the item --------------------------------------------
        item = new_context_item(
            input.scope,
            input.kind,
            input.source,
            NewItemParams(
                owner_user_id=input.owner_user_id,
                project_id=input.project_id,
                agent_id=input.agent_id,
            ),
        )
        item.title = input.title.strip()
        item.summary = input.summary
        item.tags = list(input.tags) if input.tags else []
        item.source_meta = dict(input.source_meta) if input.source_meta else {}
        item.word_count = count_words(input.content)

        # ---- Externalize if content > CONTENT_INLINE_LIMIT -------------
        mime = input.mime if input.mime else "text/plain"
        content_bytes = input.content.encode("utf-8")
        if len(content_bytes) > CONTENT_INLINE_LIMIT:
            content_uri, content_hash = self._fs.put(content_bytes, mime)
            item.content_uri = content_uri
            item.content_hash = content_hash
            item.content_mime = mime
            item.content = ""
        else:
            item.content = input.content
            # Inline: only set content_mime when caller explicitly
            # provided one — preserves Plan 1 behavior for plain-text
            # notes (content_mime stays "" by default).
            if input.mime:
                item.content_mime = input.mime

        # ---- Persist (with rollback) -----------------------------------
        try:
            self._repo.create(item)
        except Exception as exc:
            # Roll back the FileStore entries we just bumped. Without
            # this, a failed repo.create leaves orphaned refcount=1
            # blobs that nothing references. fs.delete decrements
            # refcount; when it hits 0 the file is removed.
            #
            # Two possible orphans:
            #   - item.content_uri: set when extracted text was
            #     externalized (len > CONTENT_INLINE_LIMIT). Always
            #     rolled back since Plan 1.
            #   - pdf_uri: set by the PDF branch above when storing
            #     the raw PDF blob. MUST also be rolled back, otherwise
            #     a DB write failure leaks the PDF blob forever.
            if item.content_uri:
                self._safe_delete(item.content_uri)
            if pdf_uri:
                self._safe_delete(pdf_uri)
            raise RuntimeError(f"persist item: {exc}") from exc

        # ---- Reindex FTS for externalized content ----------------------
        # The AFTER INSERT trigger on context_item wrote an FTS row
        # reading new.content, which is "" for externalized items (bytes
        # live in FileStore). Without this rewrite the item is silently
        # unsearchable via FTS — search returns 0 hits even when the
        # keyword exists in the file. reindex_fts rewrites the FTS row
        # with the hydrated content. We still have input.content in
        # memory here; no FileStore round-trip needed.
        #
        # Non-fatal: if reindex_fts fails, the item is already saved
        # and the `unictx reindex-fts` CLI command can fix it later.
        if item.content_uri:
            try:
                self._repo.reindex_fts(
                    item.id, item.title, item.summary, input.content
                )
            except Exception as exc:
                self._warn(f"warn: reindex fts for {item.id}: {exc}\n")

        # ---- Synchronous embed (optional) ------------------------------
        # Embedding failure is non-fatal — the item is already saved
        # and FTS-searchable, and the async worker (Plan 2b) will retry
        # on its next iteration. any_embedding stays 0 until the worker
        # flips it; SearchService treats 0 as "not vector-searchable".
        #
        # Embed-skip is scoped to the image-only-PDF case (pdf_uri set
        # with no extracted text AND no externalized text URI). See
        # module docstring §3.5 for the load-bearing reasoning.
        skip_embed = (
            pdf_uri != "" and item.content == "" and item.content_uri == ""
        )
        if self._embed is not None and not skip_embed:
            try:
                self._embed.embed_item(item.id, item.title, item.content)
            except Exception as exc:
                self._warn(f"warn: embed failed for {item.id}: {exc}\n")
        elif self._embed is not None and skip_embed:
            self._warn(
                f"warn: skipping embed for {item.id} "
                "(empty extracted content)\n"
            )

        return item.id

    # ---- internals ---------------------------------------------------

    def _warn(self, msg: str) -> None:
        """Write a warning line to the injected log. Best-effort."""
        # Mirror Go's fmt.Fprintf(s.log, ...). Best-effort: if the log
        # raises (e.g. closed pipe), swallow — warnings must never
        # break the ingest path.
        with contextlib.suppress(Exception):
            self._log.write(msg)

    def _safe_delete(self, uri: str) -> None:
        """fs.delete with swallowed errors — rollback must not raise.

        Go: `_ = s.fs.Delete(uri)`. Python equivalent: contextlib.suppress.
        If delete fails (e.g. FileStore corruption), we accept the
        orphaned blob rather than mask the original repo.create error.
        """
        with contextlib.suppress(Exception):
            self._fs.delete(uri)
