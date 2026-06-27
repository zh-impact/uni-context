"""``embed`` command family — model lifecycle + worker + backfill + reembed.

Faithful port of Go's ``internal/cli/embed.go``. Eight commands across
two Typer groups::

    embed model add <slug> --provider P --dim N [--base-url URL] [--api-key K]
    embed model list
    embed model remove <slug>
    embed switch <slug>
    embed backfill [--limit N] [--dry-run]
    embed worker [--interval SECONDS]
    embed reembed [--limit N] [--dry-run]
    embed status <item-id>

Behavior preserved:

  - **Embedder-disabled guard:** every subcommand checks the relevant
    service for ``None`` (when ``embedder.enabled=False`` the wire
    layer does not construct these services) and exits with a clear
    error echoing the config knob to flip.
  - **Required flags:** ``embed model add`` requires ``--provider`` and
    ``--dim`` — Go uses ``MarkFlagRequired``; we validate up front and
    surface a Typer-friendly error.
  - **Signal handling:** backfill / worker / reembed install a
    SIGINT/SIGTERM handler that sets a ``threading.Event``. The
    services' ``run()`` methods check the event between items and
    drain gracefully.
  - **Output:** tab-aligned tables for ``model list`` and ``status``
    (Go uses ``text/tabwriter``; we pad with two spaces — same look);
    one-line summary + optional failures block for backfill/reembed;
    stderr reminder after ``switch`` ("run reembed to migrate").
  - **--json:** emits machine-readable payloads (one envelope per
    subcommand). ``--json`` is the global flag from cli/app.py.

Container injection: ``_load_container`` is a module-level seam
(defaulting to ``wire(load_config(get_config_path()))``). Tests
monkeypatch it to inject a stubbed container — the same pattern
user_note.py uses (Go's ``loadAppFn`` indirection).
"""

from __future__ import annotations

import signal
import threading
from typing import Any

import typer

from unictx.cli.app import AppContainer, get_config_path, wire
from unictx.config import load as load_config
from unictx.embed.embedding_repo import EmbeddingStatus
from unictx.embed.model_registry import ModelDescriptor, ModelSpec

__all__ = [
    "embed_app",
    "format_embedding_status_row",
    "format_model_row",
    "model_add",
    "model_list",
    "model_remove",
    "run_backfill",
    "run_reembed",
    "run_status",
    "run_switch",
    "run_worker",
    "validate_model_add_args",
]


# ---------------------------------------------------------------------------
# Container seam
# ---------------------------------------------------------------------------


def _default_load_container() -> AppContainer:
    return wire(load_config(get_config_path()))


_load_container = _default_load_container


# ---------------------------------------------------------------------------
# Typer groups
# ---------------------------------------------------------------------------


embed_app = typer.Typer(
    name="embed",
    help="Manage embeddings (model lifecycle, backfill, worker, reembed).",
    no_args_is_help=True,
    add_completion=False,
)

model_app = typer.Typer(
    name="model",
    help="Manage embedding models (add/list/remove).",
    no_args_is_help=True,
    add_completion=False,
)

embed_app.add_typer(model_app, name="model")


# ---------------------------------------------------------------------------
# Helpers (pure — unit-tested directly)
# ---------------------------------------------------------------------------


_DISABLED_MSG = "embedder not enabled; set embedder.enabled=true in config"


def validate_model_add_args(
    slug: str,
    provider: str,
    dim: int,
) -> str | None:
    """Return error message if ``embed model add`` inputs are invalid.

    Mirrors Go's ``MarkFlagRequired("provider")`` and
    ``MarkFlagRequired("dim")``. ``slug`` is a required Typer Argument
    (enforced by Typer, not here).
    """
    if not provider:
        return "embed model add: --provider is required (ollama|openai)"
    if dim <= 0:
        return "embed model add: --dim must be a positive integer"
    return None


def format_model_row(m: ModelDescriptor) -> str:
    """Render one model as a tab-separated table row.

    Columns mirror Go's embedModelListCmd: SLUG, PROVIDER, DIM, VEC_TABLE,
    DEFAULT (``*`` or empty), STATUS. Padded with two spaces between
    columns (Go's tabwriter min-width 0, pad 2).
    """
    default_mark = "*" if m.is_default else ""
    return "\t".join(
        [
            m.slug,
            m.provider,
            str(m.dimension),
            m.vec_table,
            default_mark,
            m.status,
        ]
    )


_MODEL_HEADER = "\t".join(["SLUG", "PROVIDER", "DIM", "VEC_TABLE", "DEFAULT", "STATUS"])


def format_embedding_status_row(r: EmbeddingStatus) -> str:
    """Render one embedding-status row.

    Mirrors Go's embedStatusCmd: MODEL_SLUG, STATUS, ATTEMPTS, LAST_ERROR
    (truncated to 37 chars + ``...`` past 40), EMBEDDED_AT (unix epoch).

    Go truncates ``len > 40`` to ``[:37] + "..."``; we replicate the
    exact threshold so output matches byte-for-byte in tests.
    """
    err_cell = r.last_error or ""
    if len(err_cell) > 40:
        err_cell = err_cell[:37] + "..."
    # EmbeddingStatus.embedded_at is already an int unix epoch (see
    # embedding_repo.py); use it directly.
    embedded_at = r.embedded_at or 0
    return "\t".join(
        [
            r.model_slug,
            r.status,
            str(r.attempts),
            err_cell,
            str(embedded_at),
        ]
    )


_STATUS_HEADER = "\t".join(["MODEL_SLUG", "STATUS", "ATTEMPTS", "LAST_ERROR", "EMBEDDED_AT"])


# ---------------------------------------------------------------------------
# Signal-aware stop event
# ---------------------------------------------------------------------------


def _install_signal_stop_event() -> threading.Event:
    """Return an Event that gets set on SIGINT/SIGTERM.

    Mirrors Go's ``signalContext()``. The event is passed to service
    ``run()`` methods which check ``stop_event.is_set()`` between items
    and drain gracefully. The previously-installed handler is restored
    on return — but we don't return until the event is set or the
    service finishes, so this is best-effort (matches Go, which also
    leaks the signal handler over the process lifetime).

    Process-wide single-shot: each call installs a fresh handler. CLI
    invocations are one-shot processes, so we don't bother stacking
    handlers across calls.
    """
    stop = threading.Event()

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001 - signature req'd
        stop.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    return stop


# ---------------------------------------------------------------------------
# embed model add
# ---------------------------------------------------------------------------


@model_app.command("add")
def model_add(
    slug: str = typer.Argument(..., help="Model slug (e.g. bge-m3)."),
    provider: str = typer.Option("", "--provider", help="Provider (ollama|openai)."),
    base_url: str = typer.Option(
        "", "--base-url", help="Provider base URL (e.g. http://localhost:11434)."
    ),
    dim: int = typer.Option(0, "--dim", help="Embedding dimension (must match model output)."),
    api_key: str = typer.Option(
        "", "--api-key", help="API key (OpenAI-hosted); local servers ignore."
    ),
) -> None:
    """Register a new embedding model (creates its vec table)."""
    if err := validate_model_add_args(slug, provider, dim):
        typer.echo(err, err=True)
        raise typer.Exit(code=2)

    container = _load_container()
    try:
        if container.models is None:
            typer.echo(_DISABLED_MSG, err=True)
            raise typer.Exit(code=1)

        spec = ModelSpec(
            slug=slug,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            dimension=dim,
        )
        try:
            container.models.add_model(spec)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        from unictx.cli.app import is_json_mode

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json({"slug": slug, "status": "added"})
        else:
            typer.echo(f"added: {slug}")
    finally:
        container.close()


# ---------------------------------------------------------------------------
# embed model list
# ---------------------------------------------------------------------------


@model_app.command("list")
def model_list() -> None:
    """List all registered embedding models."""
    container = _load_container()
    try:
        if container.models is None:
            typer.echo(_DISABLED_MSG, err=True)
            raise typer.Exit(code=1)

        try:
            models = container.models.list_models()
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        from unictx.cli.app import is_json_mode

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json(
                {
                    "models": [
                        {
                            "slug": m.slug,
                            "provider": m.provider,
                            "dimension": m.dimension,
                            "vec_table": m.vec_table,
                            "is_default": m.is_default,
                            "status": m.status,
                        }
                        for m in models
                    ]
                }
            )
            return

        if not models:
            typer.echo("(no models registered)")
            return

        typer.echo(_MODEL_HEADER)
        for m in models:
            typer.echo(format_model_row(m))
    finally:
        container.close()


# ---------------------------------------------------------------------------
# embed model remove
# ---------------------------------------------------------------------------


@model_app.command("remove")
def model_remove(
    slug: str = typer.Argument(..., help="Model slug to remove."),
) -> None:
    """Drop a model's vec table + delete its row (refuses default + shared)."""
    container = _load_container()
    try:
        if container.models is None:
            typer.echo(_DISABLED_MSG, err=True)
            raise typer.Exit(code=1)

        try:
            container.models.remove_model(slug)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        from unictx.cli.app import is_json_mode

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json({"slug": slug, "status": "removed"})
        else:
            typer.echo(f"removed: {slug}")
    finally:
        container.close()


# ---------------------------------------------------------------------------
# embed switch
# ---------------------------------------------------------------------------


@embed_app.command("switch")
def run_switch(
    slug: str = typer.Argument(..., help="Model slug to make the active default."),
) -> None:
    """Set a registered model as the active default (atomic).

    Touches only registry metadata — the new model's vec table stays
    empty until ``embed reembed`` runs. Prints a stderr reminder so
    users don't forget to migrate existing items.
    """
    container = _load_container()
    try:
        if container.models is None:
            typer.echo(_DISABLED_MSG, err=True)
            raise typer.Exit(code=1)

        try:
            container.models.switch_model(slug)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        from unictx.cli.app import is_json_mode

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json({"slug": slug, "status": "switched"})
        else:
            # Stderr reminder mirrors Go's embedSwitchCmd. Plain-text
            # only — JSON callers consume the {slug,status} payload.
            typer.echo(
                f"Active model switched to {slug}. "
                "Run 'unictx embed reembed' to migrate existing items.",
                err=True,
            )
    finally:
        container.close()


# ---------------------------------------------------------------------------
# embed backfill
# ---------------------------------------------------------------------------


@embed_app.command("backfill")
def run_backfill(
    limit: int = typer.Option(0, "--limit", help="Max items to embed (0 = no limit)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count candidates without embedding."),
) -> None:
    """Embed all items where any_embedding=0 (idempotent)."""
    container = _load_container()
    try:
        if container.backfill is None:
            typer.echo(_DISABLED_MSG, err=True)
            raise typer.Exit(code=1)

        stop = _install_signal_stop_event()
        try:
            report = container.backfill.run(limit=limit, dry_run=dry_run, stop_event=stop)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        from unictx.cli.app import is_json_mode

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json(
                {
                    "scanned": report.scanned,
                    "embedded": report.embedded,
                    "failed": report.failed,
                    "dry_run": dry_run,
                    "failures": [{"item_id": f.item_id, "error": f.error} for f in report.failures],
                }
            )
            return

        if dry_run:
            typer.echo(f"dry run: would embed {report.scanned} items")
            return

        typer.echo(
            f"backfill complete: embedded={report.embedded} "
            f"failed={report.failed} scanned={report.scanned}"
        )
        if report.failures:
            typer.echo("failures:")
            for f in report.failures:
                typer.echo(f"  {f.item_id}: {f.error}")
    finally:
        container.close()


# ---------------------------------------------------------------------------
# embed worker
# ---------------------------------------------------------------------------


@embed_app.command("worker")
def run_worker(
    interval: float = typer.Option(
        30.0, "--interval", help="Poll interval (seconds) for failed-embedding retries."
    ),
) -> None:
    """Long-running retry loop for status=failed embeddings (Ctrl+C to stop)."""
    container = _load_container()
    try:
        if container.worker is None:
            typer.echo(_DISABLED_MSG, err=True)
            raise typer.Exit(code=1)

        stop = _install_signal_stop_event()
        # Go logs to stderr ("worker: polling every X, Ctrl+C to stop").
        # Same here — keeps stdout clean for piped usage.
        typer.echo(f"worker: polling every {interval:g}s, Ctrl+C to stop", err=True)
        try:
            container.worker.run(interval=interval, stop_event=stop)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    finally:
        container.close()


# ---------------------------------------------------------------------------
# embed reembed
# ---------------------------------------------------------------------------


@embed_app.command("reembed")
def run_reembed(
    limit: int = typer.Option(0, "--limit", help="Max items to embed (0 = no limit)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count candidates without embedding."),
) -> None:
    """Re-embed items lacking a done status row for the active model."""
    container = _load_container()
    try:
        if container.reembed is None:
            typer.echo(_DISABLED_MSG, err=True)
            raise typer.Exit(code=1)

        stop = _install_signal_stop_event()
        try:
            report = container.reembed.run(limit=limit, dry_run=dry_run, stop_event=stop)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        from unictx.cli.app import is_json_mode

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json(
                {
                    "scanned": report.scanned,
                    "embedded": report.embedded,
                    "failed": report.failed,
                    "dry_run": dry_run,
                    "failures": [{"item_id": f.item_id, "error": f.error} for f in report.failures],
                }
            )
            return

        if dry_run:
            typer.echo(f"dry run: would re-embed {report.scanned} items")
            return

        typer.echo(
            f"reembed complete: embedded={report.embedded} "
            f"failed={report.failed} scanned={report.scanned}"
        )
        if report.failures:
            typer.echo("failures:")
            for f in report.failures:
                typer.echo(f"  {f.item_id}: {f.error}")
    finally:
        container.close()


# ---------------------------------------------------------------------------
# embed status
# ---------------------------------------------------------------------------


@embed_app.command("status")
def run_status(
    item_id: str = typer.Argument(..., help="Item id to inspect."),
) -> None:
    """Show embedding status rows for an item (all models)."""
    container = _load_container()
    try:
        if container.models is None:
            typer.echo(_DISABLED_MSG, err=True)
            raise typer.Exit(code=1)

        try:
            rows = container.models.item_embedding_status(item_id)
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        from unictx.cli.app import is_json_mode

        if is_json_mode():
            from unictx.cli.output import print_json

            print_json(
                {
                    "item_id": item_id,
                    "rows": [
                        {
                            "model_slug": r.model_slug,
                            "status": r.status,
                            "attempts": r.attempts,
                            "last_error": r.last_error,
                            "embedded_at": r.embedded_at,
                        }
                        for r in rows
                    ],
                }
            )
            return

        if not rows:
            typer.echo(f"no embedding status rows for item {item_id}")
            return

        typer.echo(_STATUS_HEADER)
        for r in rows:
            typer.echo(format_embedding_status_row(r))
    finally:
        container.close()
