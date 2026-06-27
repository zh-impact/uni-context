"""CLI package — Typer app + subcommand modules.

Subcommand modules (Tasks 6.2-6.5) register their Typers against the
root ``app`` defined in :mod:`unictx.cli.app`. The registration happens
here so importing ``unictx.cli`` is sufficient to assemble the full CLI.
"""

from __future__ import annotations

from unictx.cli.app import app
from unictx.cli.user_note import user_app

# Register the `user` Typer (which itself contains the `note` Typer).
app.add_typer(user_app, name="user")

__all__ = ["app"]
