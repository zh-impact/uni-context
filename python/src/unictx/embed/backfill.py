"""BackfillService — bulk embed items where any_embedding=0.

Behavior-port of Go's ``internal/service/backfill.go``. First-time
embed sweep: finds items that have never been embedded and runs
EmbedService on each.

Plan §Python Conventions adaptations:
  - ctx dropped (Python is sync). Cancellation via ``threading.Event``.
  - Go's ``Run(ctx, limit, dryRun) (BackfillReport, error)`` →
    ``run(limit, dry_run) -> BackfillReport``. List-failed error raises.
  - ``log`` defaults to ``sys.stderr``; tests pass ``StringIO``.

Idempotent (Go backfill.go:13-15 preserved): items already embedded
(``any_embedding=1``) are excluded by the ``ItemFilter.any_embedding=0``
pre-filter, so they never enter iteration. Re-runs only pick up
newly-created items.

Failure policy (Go backfill.go:90-97 preserved): per-item embed errors
are recorded in ``BackfillReport.failures`` and the run continues.
``run`` itself does NOT raise on per-item failures; the only raise is
from the initial ``repo.list`` call (e.g. DB unavailable).
"""

from __future__ import annotations

import contextlib
import sys
import threading
from dataclasses import dataclass, field
from typing import IO

from unictx.embed.service import EmbedService
from unictx.items.repo import ContextRepo, ItemFilter

__all__ = ["BackfillService", "BackfillReport", "BackfillFailure"]


@dataclass(slots=True)
class BackfillFailure:
    """One per-item embed error during a backfill run.

    Aggregated in ``BackfillReport.failures`` so the CLI can surface
    them in --json output or human-readable summary.
    """

    item_id: str
    error: str


@dataclass(slots=True)
class BackfillReport:
    """Summarizes one ``BackfillService.run`` invocation.

    No ``skipped`` field: the ``any_embedding=0`` pre-filter excludes
    already-embedded items before iteration begins, so there is nothing
    to skip. Mirrors Go's BackfillReport (backfill.go:43-48).
    """

    scanned: int = 0
    embedded: int = 0
    failed: int = 0
    failures: list[BackfillFailure] = field(default_factory=list)


class BackfillService:
    """Bulk-embeds items where ``any_embedding=0``.

    Construction is cheap (no I/O). The ``embed_svc`` is the
    ``EmbedService`` — it handles status-row + vector writes internally.
    """

    def __init__(
        self,
        repo: ContextRepo,
        embed_svc: EmbedService,
        log: IO[str] | None = None,
    ) -> None:
        self._repo = repo
        self._embed = embed_svc
        self._log: IO[str] = log if log is not None else sys.stderr

    def run(
        self,
        limit: int = 0,
        dry_run: bool = False,
        stop_event: threading.Event | None = None,
    ) -> BackfillReport:
        """Iterate items where ``any_embedding=0`` and embed each.

        Per item:
          - ``dry_run=True``: increment ``scanned`` only (no embed,
            no status row).
          - ``dry_run=False``: call EmbedService.embed_item; on failure
            record a ``BackfillFailure`` and continue; on success
            increment ``embedded``.

        ``limit<=0`` means no limit (passes through to repo.list, which
        treats 0 as "default page size" per the ContextRepo contract).

        Progress is logged every 100 items. Mirrors Go backfill.go:100-102.
        """
        report = BackfillReport()

        # Filter to unembedded items only. any_embedding is tri-state:
        # None = no filter; 0 = only items NOT yet embedded; 1 = only
        # items already embedded. We want 0 (Go backfill.go:68-73).
        items, _next_cursor = self._repo.list(
            ItemFilter(any_embedding=0, limit=limit)
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
                    BackfillFailure(item_id=item.id, error=str(exc))
                )
                continue
            report.embedded += 1

            if (i + 1) % 100 == 0:
                self._warn(f"backfill: {i + 1} items processed\n")

        return report

    # ---- internals ---------------------------------------------------

    def _warn(self, msg: str) -> None:
        """Write a warning/progress line to the injected log. Best-effort."""
        with contextlib.suppress(Exception):
            self._log.write(msg)
