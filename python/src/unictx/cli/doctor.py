"""``doctor`` command — setup sanity check.

Faithful port of Go's ``internal/cli/doctor.go``. One command::

    unictx doctor

Prints config/data/db/filestore/user paths, the schema version (from
``SchemaMeta`` via :class:`DiagnosticService`), and an embedder check
(:meth:`DiagnosticService.ping_embedder` — exercises the live service
with a one-token embed when enabled). Overall status: OK / FAIL. A
failed embedder check flips the status to FAIL and exits non-zero so
scripts and CI can detect the broken state.

``--json`` emits the same payload as a structured object.
"""

from __future__ import annotations

from typing import Any

import typer

from unictx.cli.app import AppContainer, get_config_path, wire
from unictx.config import Config
from unictx.config import load as load_config

__all__ = ["doctor", "format_doctor_line"]


def _default_load_container() -> AppContainer:
    return wire(load_config(get_config_path()))


_load_container = _default_load_container


def format_doctor_line(label: str, value: object) -> str:
    """Render one doctor output line, label left-padded to 15 chars.

    Mirrors Go's ``fmt.Printf("%-15s %s\n", label, value)`` — label
    column is 15 chars wide, value follows after a single space.
    """
    return f"{label:<15} {value}"


def doctor() -> None:
    """Check that uni-context is set up correctly.

    Registered as ``app.command(name="doctor")(doctor)`` in cli/__init__.py.
    """
    from unictx.cli.app import is_json_mode

    container = _load_container()
    try:
        cfg: Config = container.config

        # Path diagnostics — same fields Go prints, sourced from cfg
        # (Python's Config lacks db_path/filestore_dir helpers, so we
        # inline the same joins wire() uses).
        db_path = cfg.data_dir / "unictx.db"
        filestore_dir = cfg.data_dir / "filestore"

        # Schema version (always available — SchemaMetaImpl is constructed
        # unconditionally in wire()).
        try:
            schema_version = container.diagnostics.schema_version()
        except Exception as exc:
            typer.echo(f"read schema version: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        # Embedder check. DiagnosticService.ping_embedder:
        #   - returns (zero, False) when no embedder wired (Plan 1)
        #   - returns (ModelInfo, True) when embedder answered
        #   - raises when the ping failed (Go's "FAIL" branch).
        embedder_state: str
        embedder_detail: str = ""
        check_failed = False
        try:
            info, enabled = container.diagnostics.ping_embedder()
            if enabled:
                embedder_state = "OK"
                embedder_detail = f"{info.slug}, {info.dimension}-dim"
            else:
                embedder_state = "disabled"
                embedder_detail = "Plan 1 mode; set embedder.enabled=true to enable"
        except Exception as exc:
            embedder_state = "FAIL"
            embedder_detail = str(exc)
            check_failed = True

        status = "FAIL" if check_failed else "OK"

        if is_json_mode():
            from unictx.cli.output import print_json

            payload: dict[str, Any] = {
                "config_path": str(get_config_path()) if get_config_path() else None,
                "data_dir": str(cfg.data_dir),
                "db_path": str(db_path),
                "filestore_dir": str(filestore_dir),
                "user_id": cfg.user.id,
                "schema_version": schema_version,
                "embedder": {
                    "state": embedder_state,
                    "detail": embedder_detail,
                },
                "status": status,
            }
            print_json(payload)
            return

        # Plain text — label-value pairs, 15-char label column.
        typer.echo(format_doctor_line("config path:", get_config_path() or "(default)"))
        typer.echo(format_doctor_line("data dir:", cfg.data_dir))
        typer.echo(format_doctor_line("db path:", db_path))
        typer.echo(format_doctor_line("filestore dir:", filestore_dir))
        typer.echo(format_doctor_line("user id:", cfg.user.id))
        typer.echo(format_doctor_line("schema version:", schema_version))
        typer.echo(f"  embedder: {embedder_state} ({embedder_detail})")
        typer.echo(format_doctor_line("status:", status))

        if check_failed:
            raise typer.Exit(code=1)
    finally:
        container.close()
