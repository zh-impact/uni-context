"""ReindexFTSService — bulk-rewrite context_fts rows for externalized items.

Behavior-port of Go's ``internal/service/reindex_fts.go``. Walks items
whose content was externalized (> ``CONTENT_INLINE_LIMIT``), hydrates
the content from FileStore, and calls ``repo.reindex_fts`` to rewrite
the FTS row with the real bytes.

Why this exists: the AFTER INSERT trigger on ``context_item`` reads
``new.content`` when writing the FTS row; for externalized items
``new.content`` is ``""`` so the FTS row was indexed empty, making
the item invisible to ``search``. This service fixes the gap as a
one-shot maintenance command.

Constructed unconditionally (independent of ``embedder.enabled``)
because FTS search works in Plan 1 too — the bug is not embedding-
specific.

Plan §Python Conventions adaptations:
  - ctx dropped (Python is sync). Cancellation via ``threading.Event``.
  - Go's ``Run(ctx, limit, dryRun) (ReindexReport, error)`` →
    ``run(limit, dry_run) -> ReindexReport``. List error raises.
  - ``log`` defaults to ``sys.stderr``; tests pass ``StringIO``.

Idempotent (Go reindex_fts.go:22-24 preserved): ReindexFTS uses a
delete-then-insert pattern that yields one FTS row per call regardless
of how many times it runs.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from dataclasses import dataclass, field
from typing import IO

from unictx.items.repo import ContextRepo, ItemFilter
from unictx.storage.filestore import FileStore

__all__ = ["ReindexFTSService", "ReindexReport", "ReindexFailure"]


@dataclass(slots=True)
class ReindexFailure:
    """One per-item error during a reindex-fts run.

    Aggregated in ``ReindexReport.failures`` so the CLI can surface
    them in --json output or human-readable summary.
    """

    item_id: str
    error: str


@dataclass(slots=True)
class ReindexReport:
    """Summarizes one ``ReindexFTSService.run`` invocation.

    Mirrors Go's ReindexReport (reindex_fts.go:46-51). ``scanned``
    counts the externalized candidates found; ``reindexed`` counts
    successful rewrites; ``failed`` counts per-item failures
    (FileStore miss, FTS rewrite error).
    """

    scanned: int = 0
    reindexed: int = 0
    failed: int = 0
    failures: list[ReindexFailure] = field(default_factory=list)


# Page size for the list pagination. 200 matches Go; large enough that
# a typical corpus completes in 1-2 pages, small enough to keep per-page
# memory bounded.
_PAGE_SIZE = 200


class ReindexFTSService:
    """Bulk-rewrites context_fts rows for externalized items.

    Construction is cheap (no I/O). The service walks items via
    ``repo.list`` (paginated), filters to externalized candidates
    (``content_uri != ""`` AND ``content == ""``), hydrates their
    content via ``fs.get``, and calls ``repo.reindex_fts``.

    Inline items are skipped — the AFTER INSERT trigger already
    indexed them correctly.
    """

    def __init__(
        self,
        repo: ContextRepo,
        fs: FileStore,
        log: IO[str] | None = None,
    ) -> None:
        self._repo = repo
        self._fs = fs
        self._log: IO[str] = log if log is not None else sys.stderr

    def run(
        self,
        limit: int = 0,
        dry_run: bool = False,
        stop_event: threading.Event | None = None,
    ) -> ReindexReport:
        """Iterate items and rewrite FTS rows for externalized ones.

        Per externalized item (``content_uri != ""`` AND ``content == ""``):
          - ``dry_run=True``: increment ``scanned`` only (no fs.get,
            no reindex_fts).
          - ``dry_run=False``: hydrate via ``fs.get``, call
            ``repo.reindex_fts``; on failure append to ``failures`` and
            continue; on success increment ``reindexed``.

        ``limit<=0`` means no limit. List pages with the default cursor;
        this is a one-shot maintenance command so simplicity wins over
        throughput.

        ``run`` itself does NOT raise on per-item failures; the only
        raise is from the initial ``repo.list`` call (e.g. DB unavailable).
        """
        report = ReindexReport()

        page_size = _PAGE_SIZE
        if 0 < limit < page_size:
            page_size = limit

        cursor = ""
        while True:
            if stop_event is not None and stop_event.is_set():
                return report

            if limit > 0 and report.scanned >= limit:
                break

            page_limit = page_size
            if limit > 0:
                remaining = limit - report.scanned
                if remaining < page_limit:
                    page_limit = remaining

            items, next_cursor = self._repo.list(
                ItemFilter(limit=page_limit, cursor=cursor)
            )
            if not items:
                break

            for item in items:
                # Inline items are already correctly indexed by the trigger.
                if item.content_uri == "" or item.content:
                    continue
                # Cap the candidate count even when list returns more
                # items than the limit (the test fake ignores Limit; real
                # sqlite honors it but a page may still overshoot if
                # items were added between pages).
                if limit > 0 and report.scanned >= limit:
                    return report
                report.scanned += 1

                if dry_run:
                    continue

                if stop_event is not None and stop_event.is_set():
                    return report

                try:
                    data = self._fs.get(item.content_uri)
                except Exception as exc:
                    report.failed += 1
                    report.failures.append(
                        ReindexFailure(item_id=item.id, error=f"hydrate: {exc}")
                    )
                    continue

                try:
                    self._repo.reindex_fts(
                        item.id, item.title, item.summary, data.decode("utf-8", errors="replace")
                    )
                except Exception as exc:
                    report.failed += 1
                    report.failures.append(
                        ReindexFailure(item_id=item.id, error=str(exc))
                    )
                    continue
                report.reindexed += 1

            if next_cursor == "":
                break
            cursor = next_cursor

            self._warn(f"reindex-fts: {report.scanned} items scanned\n")

        return report

    # ---- internals ---------------------------------------------------

    def _warn(self, msg: str) -> None:
        """Write a progress line to the injected log. Best-effort."""
        with contextlib.suppress(Exception):
            self._log.write(msg)
