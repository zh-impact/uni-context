"""EmbedService — write embeddings + status rows + any_embedding flag.

Behavior-port of Go's ``internal/service/embed.go``. Reached via
Worker / Backfill / Reembed (NOT via IngestService — its embed-skip
short-circuits before calling EmbedService; see items/ingest.py §3.5).

Plan §Python Conventions adaptations:
  - ctx dropped (Python is sync).
  - Go's ``Embed(ctx, itemID, title, content) error`` →
    ``embed_item(item_id, title, content) -> None`` (errors raise).
  - ``model_slug`` is **derived internally** from
    ``embedder.model().slug`` (Go embed.go:69); NOT a parameter.
  - ``log`` defaults to ``sys.stderr``; tests pass ``StringIO``.

Status row policy (Go embed.go:67-125):
  - Written on EVERY attempt via ``EmbeddingRepo.upsert_status``.
  - Success → ``status='done'``, ``err_str=""``.
  - Failure → ``status='failed'``, ``err_str=error text``.
  - Status-write failure is logged but NEVER masks the original result.

Vector write policy:
  - Only on success, AFTER ``embedder.embed`` succeeds AND exactly 1
    vector returned.
  - ``VectorStore.put(model_slug, item_id, vector)``.
  - Failure here records ``status='failed'`` and raises.

any_embedding flag policy — **deviation from Go** (Plan §3.6 mandates):
  Go returns an error if either repo.get or repo.update fails during
  the flag flip (embed.go:107-120). The brief overrides this:
  "Flag-write failure is non-fatal: status stays 'done' (vec row is
  the source of truth for 'embedded'), warning logged."

  Concretely: BOTH the load-item step and the update-item step are
  non-fatal. The vec row IS the source of truth for "embedded"; the
  flag is a perf optimization, not correctness. A warning is logged
  so operators see observability gaps; ``embed_item`` returns ``None``.
"""

from __future__ import annotations

import contextlib
import sys
from typing import IO

from unictx.embed.embedder import Embedder
from unictx.embed.embedding_repo import EmbeddingRepo
from unictx.items.repo import ContextRepo
from unictx.search.vectorstore import VectorStore
from unictx.storage.filestore import FileStore

__all__ = ["EmbedService"]


class EmbedService:
    """Writes embeddings + status rows + any_embedding flag.

    Construction is cheap (no I/O). All five dependencies are required:
      - ``embedder``: produces vectors from text (port.Embedder).
      - ``vs``: stores vectors keyed by ``(model, item_id)`` (port.VectorStore).
      - ``repo``: fetches items for hydration + flag flip (port.ContextRepo).
      - ``fs``: hydrates content from ContentURI (port.FileStore).
      - ``emb_repo``: writes status rows on every attempt (port.EmbeddingRepo).
    """

    def __init__(
        self,
        embedder: Embedder,
        vs: VectorStore,
        repo: ContextRepo,
        fs: FileStore,
        emb_repo: EmbeddingRepo,
        log: IO[str] | None = None,
    ) -> None:
        self._embedder = embedder
        self._vs = vs
        self._repo = repo
        self._fs = fs
        self._emb_repo = emb_repo
        # Default to stderr so prod warnings surface without forcing
        # every caller to pass a writer. Tests inject StringIO to
        # assert on warn-and-continue paths (status-write failure,
        # flag-write failure).
        self._log: IO[str] = log if log is not None else sys.stderr

    def embed_item(self, item_id: str, title: str, content: str) -> None:
        """Compute and store an embedding for ``item_id``.

        See module docstring for the full status/vector/flag policy.

        Raises:
            RuntimeError: on hydration failure (wrapped), empty text,
                embedder failure, vector count mismatch, or
                VectorStore.put failure. Status row is ``'failed'`` in
                all these cases. NEVER raises on any_embedding flag
                flip failure — that path is non-fatal per Plan §3.6
                (vec row is source of truth; flag is perf only).
        """
        # Derived from the embedder — NOT a parameter. Go embed.go:69.
        model_slug = self._embedder.model().slug

        # Hydrate if the caller didn't supply content. IngestService
        # calls embed_item(item.id, title, "") for externalized items
        # (item.content was cleared after fs.put). Backfill may pass
        # content directly to skip the round-trip.
        hydrated = content
        if hydrated == "":
            try:
                hydrated = self._hydrate_content(item_id)
            except Exception as exc:
                # Hydration failure is recoverable by the worker later;
                # record status='failed' with the ORIGINAL error text
                # (not the wrapped version we raise).
                self._record_status(item_id, model_slug, "failed", str(exc))
                raise RuntimeError(f"hydrate content for {item_id}: {exc}") from exc

        # Title + "\n\n" + content composition. Empty after strip means
        # neither title nor content contributed text — a title-only
        # embed with empty title is meaningless, and an empty-content
        # item with no title produces a zero-vector embed. Go embed.go:84.
        text = (title + "\n\n" + hydrated).strip()
        if text == "":
            err = RuntimeError(f"embed: empty text for item {item_id}")
            self._record_status(item_id, model_slug, "failed", str(err))
            raise err

        # Embed. The embed call is the slowest step (HTTP to backend);
        # failures here are usually transient (backend down, timeout).
        try:
            vecs = self._embedder.embed([text])
        except Exception as exc:
            self._record_status(item_id, model_slug, "failed", str(exc))
            raise RuntimeError(f"embed item {item_id}: {exc}") from exc

        if len(vecs) != 1:
            err = RuntimeError(
                f"embedder returned {len(vecs)} vectors, expected 1"
            )
            self._record_status(item_id, model_slug, "failed", str(err))
            raise err

        # Vector write. On failure: record 'failed' + raise. No flag
        # flip attempted (the item isn't vector-searchable anyway).
        try:
            self._vs.put(model_slug, item_id, vecs[0])
        except Exception as exc:
            self._record_status(item_id, model_slug, "failed", str(exc))
            raise RuntimeError(f"store vector for {item_id}: {exc}") from exc

        # Flip any_embedding=1 so SearchService knows this item is
        # vector-searchable. DEVIATION from Go (Plan §3.6): both
        # load-item and update-item failures here are NON-FATAL. Vec
        # row is already written — it IS the source of truth for
        # "embedded". The flag is a perf optimization. Surface a
        # warning so operators see observability gaps; do NOT raise.
        try:
            item = self._repo.get(item_id)
        except Exception as exc:
            self._warn(
                f"warn: failed to load item for any_embedding flag "
                f"({item_id}): {exc}\n"
            )
            self._record_status(item_id, model_slug, "done", "")
            return

        item.any_embedding = 1
        try:
            self._repo.update(item)
        except Exception as exc:
            self._warn(
                f"warn: failed to flip any_embedding flag for "
                f"{item_id}: {exc}\n"
            )

        self._record_status(item_id, model_slug, "done", "")

    # ---- helpers -----------------------------------------------------

    def _hydrate_content(self, item_id: str) -> str:
        """Return the item's content, hydrating from FileStore if needed.

        - If ``item.content`` is set, return it (inline case).
        - Else if ``item.content_uri`` is set, fetch bytes from
          FileStore and decode UTF-8 (externalized case).
        - Else return ``""`` — caller treats as title-only embed
          (text composition strips to title; if title also empty,
          embed_item records status='failed' + raises).

        Raises the underlying ``repo.get`` / ``fs.get`` error directly;
        caller catches and records status. Go embed.go:131-147.

        Decode policy: ``errors="replace"`` so malformed UTF-8 in
        extracted text doesn't crash the embed pipeline. Go's
        ``string(bytes)`` is byte-faithful; Python's ``decode("utf-8")``
        raises on bad bytes, so we opt for ``replace`` to preserve
        Go's "never crash on content encoding" semantics.
        """
        item = self._repo.get(item_id)
        if item.content:
            return item.content
        if item.content_uri == "":
            return ""  # neither inline nor externalized; title-only embed
        raw = self._fs.get(item.content_uri)
        return raw.decode("utf-8", errors="replace")

    def _record_status(
        self,
        item_id: str,
        model_slug: str,
        status: str,
        err_str: str,
    ) -> None:
        """Wrap ``emb_repo.upsert_status`` with log-on-failure.

        Status-row write failure must NEVER mask the original embed
        result — best-effort via ``contextlib.suppress``. Warning
        logged so operators see observability gaps (a missing status
        row means the worker can't track retries for this item).
        """
        try:
            self._emb_repo.upsert_status(item_id, model_slug, status, err_str)
        except Exception as exc:
            self._warn(
                f"warn: failed to record embedding status for "
                f"{item_id}: {exc}\n"
            )

    def _warn(self, msg: str) -> None:
        """Write a warning line to the injected log. Best-effort.

        Mirror Go's ``fmt.Fprintf(s.log, ...)`` semantics: warnings
        must never break the embed path. ``contextlib.suppress``
        swallows closed-pipe / disk-full / encoding errors.
        """
        with contextlib.suppress(Exception):
            self._log.write(msg)
