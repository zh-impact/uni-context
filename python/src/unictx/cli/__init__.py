"""CLI package — Typer app + subcommand modules.

Subcommand modules (Tasks 6.2-6.5) register against the root ``app``
defined in :mod:`unictx.cli.app`. The registration happens here so
importing ``unictx.cli`` is sufficient to assemble the full CLI.

Registration styles:
  - ``user`` → ``app.add_typer(user_app, name="user")`` (nested Typer
    with subcommands: ``user note add|get|list|delete``).
  - ``search`` → ``app.command(name="search")(search)`` (direct command).
    See :mod:`unictx.cli.search` module docstring for why search is a
    direct command rather than a sub-Typer with a callback (Typer's
    variadic-positional parsing quirk).
"""

from __future__ import annotations

from unictx.cli.app import app
from unictx.cli.search import search as _search_cmd
from unictx.cli.user_note import user_app

# Register the `user` Typer (which itself contains the `note` Typer).
app.add_typer(user_app, name="user")
# `search` registers as a direct top-level command. Function imported
# under a private alias above so the public ``unictx.cli.search`` name
# remains bound to the submodule (tests do ``import unictx.cli.search
# as search_mod`` to monkeypatch ``_load_container``).
app.command(name="search")(_search_cmd)

__all__ = ["app"]
