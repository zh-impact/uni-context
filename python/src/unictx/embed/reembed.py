"""ReembedService — bulk re-embed items under the active model.

Behavior-port of Go's ``internal/service/reembed.go``. Differs from
BackfillService in filter: BackfillService targets items where
``any_embedding=0`` (first-time embed); ReembedService targets items
that lack a ``status='done'`` row for the active model (migration to
a new model after ``embed switch``).

Plan §Python Conventions adaptations:
  - ctx dropped (Python is sync). Cancellation via ``threading.Event``.
  - Go's ``Run(ctx, limit, dryRun) (ReembedReport, error)`` →
    ``run(limit, dry_run) -> ReembedReport``. List error raises.
  - ``log`` defaults to ``sys.stderr``; tests pass ``StringIO``.

Idempotent (Go reembed.go:17-19 preserved): re-runs skip items already
done for the active model. Resumable: failed items get status='failed'
rows and are picked up by WorkerService (which is model-agnostic).
"""

from __future__ import annotations

import contextlib
import sys
import threading
from dataclasses import dataclass, field
from typing import IO

from unictx.embed.embedder import ModelInfo
from unictx.embed.service import EmbedService
from unictx.items.repo import ContextRepo, ItemFilter

__all__ = ["ReembedService", "ReembedReport", "ReembedFailure"]


@dataclass(slots=True)
class ReembedFailure:
    """One per-item embed error during a reembed run."""

    item_id: str
    error: str


@dataclass(slots=True)
class ReembedReport:
    """Summarizes one ``ReembedService.run`` invocation.

    Mirrors Go's ReembedReport (reembed.go:47-52). ``scanned`` counts
    candidates found (no done row for active model); ``embedded``
    counts successful embeds; ``failed`` counts per-item failures.
    """

    scanned: int = 0
    embedded: int = 0
    failed: int = 0
    failures: list[ReembedFailure] = field(default_factory=list)


class ReembedService:
    """Bulk re-embeds items lacking a status='done' row for the active model.

    Construction is cheap (no I/O). The ``active`` ModelInfo identifies
    the currently-wired embedder — its slug becomes the
    ``ItemFilter.not_done_for_model`` filter key.

    Idempotent + resumable. Re-runs after a partial success skip items
    already done for the active model. Failures get status='failed'
    rows and are picked up by the WorkerService.
    """

    def __init__(
        self,
        repo: ContextRepo,
        embed_svc: EmbedService,
        active: ModelInfo,
        log: IO[str] | None = None,
    ) -> None:
        self._repo = repo
        self._embed = embed_svc
        self._active = active
        self._log: IO[str] = log if log is not None else sys.stderr

    def run(
        self,
        limit: int = 0,
        dry_run: bool = False,
        stop_event: threading.Event | None = None,
    ) -> ReembedReport:
        """Iterate items lacking a status='done' row for the active model.

        Per item:
          - ``dry_run=True``: increment ``scanned`` only.
          - ``dry_run=False``: call EmbedService.embed_item; on failure
            record a ``ReembedFailure`` and continue; on success
            increment ``embedded``.

        ``limit<=0`` means no limit.

        Progress is logged every 100 items. Mirrors Go reembed.go:97-99.
        """
        report = ReembedReport()

        items, _next_cursor = self._repo.list(
            ItemFilter(not_done_for_model=self._active.slug, limit=limit)
        )

        for i, item in enumerate(items):
            if stop_event is not None and stop_event.is_set():
                return report

            report.scanned += 1
            if dry_run:
                continue

            try:
                self._embed.embed_item(item.id, item.title, item.content)
            except Exception as exc:
                report.failed += 1
                report.failures.append(
                    ReembedFailure(item_id=item.id, error=str(exc))
                )
                continue
            report.embedded += 1

            if (i + 1) % 100 == 0:
                self._warn(f"reembed: {i + 1} items processed\n")

        return report

    # ---- internals ---------------------------------------------------

    def _warn(self, msg: str) -> None:
        """Write a warning/progress line to the injected log. Best-effort."""
        with contextlib.suppress(Exception):
            self._log.write(msg)
