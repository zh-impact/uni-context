"""WorkerService — polls for status='failed' embeddings and retries them.

Behavior-port of Go's ``internal/service/worker.go``. Long-running poll
loop; the CLI's signal handler flips a ``threading.Event`` to stop.

Plan §Python Conventions adaptations:
  - ctx dropped (Python is sync). Cancellation via ``threading.Event``:
    pass an event the CLI signal handler can set(); ``run`` checks
    ``is_set()`` before each iteration and per-item.
  - Go's ``RunOneIteration(ctx) (int, error)`` → ``run_one_iteration``
    returns ``int``; list-failed error raises.
  - ``log`` defaults to ``sys.stderr``; tests pass ``StringIO``.

Plan 2b scope (preserved): fixed poll interval, no exponential
backoff, no max-attempts cap. A row stays 'failed' until it succeeds;
operators can DELETE the row manually to skip an unrecoverable item
(e.g. wrong model dimension).
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from typing import IO

from unictx.embed.embedding_repo import EmbeddingRepo
from unictx.embed.service import EmbedService
from unictx.items.repo import ContextRepo

__all__ = ["WorkerService", "WORKER_BATCH_SIZE", "DEFAULT_INTERVAL"]


# Caps how many failed rows one iteration pulls. 100 is large enough
# to drain a typical backlog quickly but small enough to keep each
# iteration bounded so the loop stays responsive to cancellation.
WORKER_BATCH_SIZE = 100

# Default poll interval when caller passes interval<=0. Mirrors Go's
# worker.go:94 default. 30s is a deliberate compromise: fast enough
# that transient backend failures recover quickly, slow enough that
# steady-state cost is negligible.
DEFAULT_INTERVAL = 30.0


class WorkerService:
    """Polls ``context_embedding`` for status='failed' rows and retries.

    Construction is cheap (no I/O). The ``embed_svc`` is the
    ``EmbedService`` — it handles status-row updates internally (writes
    ``'done'`` on success, ``'failed' + attempts++`` on failure), so
    ``run_one_iteration`` MUST NOT call ``emb_repo.upsert_status``
    directly. Doing so would double-write (EmbedService + worker) and
    skew the attempts counter.
    """

    def __init__(
        self,
        repo: ContextRepo,
        emb_repo: EmbeddingRepo,
        embed_svc: EmbedService,
        log: IO[str] | None = None,
    ) -> None:
        self._repo = repo
        self._emb_repo = emb_repo
        self._embed = embed_svc
        self._log: IO[str] = log if log is not None else sys.stderr

    def run_one_iteration(
        self, stop_event: threading.Event | None = None
    ) -> int:
        """Process one batch of failed embeddings. Returns attempt count.

        Pre-iteration check: if ``stop_event`` is set on entry, returns
        0 immediately (mirrors Go's pre-cancelled ctx short-circuit).
        Per-item check: ``stop_event.is_set()`` between items returns
        the partial count.

        EmbedService.embed_item handles the status row update on each
        retry (writes 'done' on success, 'failed' + attempts++ on
        failure); this method never calls emb_repo.upsert_status.

        Item-vanished race: an item deleted between failure and retry
        logs a warning and is skipped (the ON DELETE CASCADE on
        context_embedding.item_id should have removed the row already,
        but defensive — matches Go worker.go:65-72).
        """
        failed = self._emb_repo.list_failed(WORKER_BATCH_SIZE)

        processed = 0
        for st in failed:
            # Pre-iteration check (Go's `select { case <-ctx.Done() }`)
            # expressed as a non-blocking event probe. Set by the CLI's
            # signal handler for graceful Ctrl+C exit.
            if stop_event is not None and stop_event.is_set():
                return processed

            try:
                item = self._repo.get(st.item_id)
            except Exception as exc:
                # Item was deleted between failure and retry. The
                # ON DELETE CASCADE on context_embedding.item_id
                # should have removed the row already, but defensive.
                self._warn(f"worker: item {st.item_id} vanished: {exc}\n")
                continue

            # EmbedService.embed_item handles status row update
            # internally (writes 'done' on success, 'failed' +
            # attempts++ on failure).
            try:
                self._embed.embed_item(item.id, item.title, item.content)
            except Exception as exc:
                self._warn(
                    f"worker: retry failed for {item.id} "
                    f"(attempt {st.attempts + 1}): {exc}\n"
                )
            processed += 1
        return processed

    def run(
        self,
        interval: float,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Poll loop. Logs progress each iteration. Returns on ``stop_event``.

        Pre-iteration check: if ``stop_event`` is set on entry, returns
        immediately without doing any work — mirrors Go's pre-cancelled
        ctx short-circuit (worker.go:95-100) so tests and fast Ctrl+C
        right after startup don't fire RunOneIteration once needlessly.

        Interval <= 0 falls back to DEFAULT_INTERVAL (30s).

        Sleep uses ``stop_event.wait(timeout=interval)`` — returns True
        if the event was set during the wait (cancellation), False on
        timeout. This makes the loop responsive mid-sleep instead of
        blocking the full interval.
        """
        if interval <= 0:
            interval = DEFAULT_INTERVAL

        while True:
            if stop_event is not None and stop_event.is_set():
                return

            try:
                processed = self.run_one_iteration(stop_event)
            except Exception as exc:
                # List-failed error is unrecoverable for this iteration;
                # log + continue loop so the worker doesn't die on a
                # transient DB hiccup. The Go original returns the
                # error; we log + keep going (CLI can Ctrl+C if needed).
                self._warn(f"worker: iteration failed: {exc}\n")
                processed = 0

            self._warn(
                f"worker: processed {processed} items, sleeping {interval}\n"
            )

            if stop_event is not None:
                # wait() returns True iff the event was set during the
                # wait — that's our cancellation signal. False means
                # the timeout elapsed normally.
                if stop_event.wait(timeout=interval):
                    return
            else:
                time.sleep(interval)

    # ---- internals ---------------------------------------------------

    def _warn(self, msg: str) -> None:
        """Write a warning line to the injected log. Best-effort."""
        with contextlib.suppress(Exception):
            self._log.write(msg)
