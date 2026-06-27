"""``reindex-fts`` command — heal externalized items' FTS rows.

Faithful port of Go's ``internal/cli/reindex_fts.go``. One command::

    unictx reindex-fts [--limit N] [--dry-run]

Walks all items, hydrates externalized content from FileStore, and
rewrites the FTS row so ``search`` can find it. Idempotent: the
underlying :class:`ReindexFTSService` uses a delete-then-insert pattern
that produces one FTS row per item regardless of how many times it
runs. Safe to re-run after interruptions.

Inline items are skipped — their FTS rows were correctly populated by
the AFTER INSERT trigger.

Used to heal the historical bug where items ingested before
``IngestService`` called ``ReindexFTS`` on Create had their FTS row
indexed empty (the trigger read ``new.content`` which was empty for
externalized items — real bytes lived in FileStore).
"""

from __future__ import annotations

import signal
import threading
from typing import Any

import typer

from unictx.cli.app import AppContainer, get_config_path, wire
from unictx.config import load as load_config

__all__ = ["reindex_fts"]


def _default_load_container() -> AppContainer:
    return wire(load_config(get_config_path()))


_load_container = _default_load_container


def _install_signal_stop_event() -> threading.Event:
    """Return an Event set on SIGINT/SIGTERM (mirrors embed_cmd pattern).

    Reindex over a large corpus can take a while; Ctrl+C drains
    gracefully mid-scan instead of cutting off (which would leave
    half-rewritten rows). Same handler pattern as embed_cmd — kept
    inline to keep this file self-contained.
    """
    stop = threading.Event()

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001 - signature req'd
        stop.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    return stop


def reindex_fts(
    limit: int = typer.Option(0, "--limit", help="Max items to scan (0 = no limit)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Count externalized candidates without rewriting."
    ),
) -> None:
    """Rewrite FTS rows for externalized items (heal pre-fix backfill).

    Registered as ``app.command(name="reindex-fts")(reindex_fts)`` in
    cli/__init__.py.
    """
    from unictx.cli.app import is_json_mode

    container = _load_container()
    try:
        stop = _install_signal_stop_event()
        try:
            report = container.reindex_fts.run(limit=limit, dry_run=dry_run, stop_event=stop)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json(
                {
                    "scanned": report.scanned,
                    "reindexed": report.reindexed,
                    "failed": report.failed,
                    "dry_run": dry_run,
                    "failures": [{"item_id": f.item_id, "error": f.error} for f in report.failures],
                }
            )
            return

        if dry_run:
            typer.echo(f"dry run: would reindex {report.scanned} externalized items")
            return

        typer.echo(
            f"reindex complete: reindexed={report.reindexed} "
            f"failed={report.failed} scanned={report.scanned}"
        )
        if report.failures:
            typer.echo("failures:")
            for f in report.failures:
                typer.echo(f"  {f.item_id}: {f.error}")
    finally:
        container.close()
