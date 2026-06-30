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

from unictx.cli.access_cmd import access_app
from unictx.cli.app import app
from unictx.cli.doctor import doctor as _doctor_cmd
from unictx.cli.embed_cmd import embed_app
from unictx.cli.reindex_fts_cmd import reindex_fts as _reindex_fts_cmd
from unictx.cli.search import search as _search_cmd
from unictx.cli.user_note import user_app

# Register the `user` Typer (which itself contains the `note` Typer).
app.add_typer(user_app, name="user")
# `embed` registers as a Typer with subcommands (model/switch/backfill/
# worker/reembed/status).
app.add_typer(embed_app, name="embed")
# `access` registers as a Typer with subcommands (grant add/list/remove).
app.add_typer(access_app, name="access")
# Direct top-level commands. Imported under private aliases so the
# public ``unictx.cli.<name>`` binding stays on the submodule (tests
# do ``import unictx.cli.search as search_mod`` to monkeypatch the
# ``_load_container`` seam).
app.command(name="search")(_search_cmd)
app.command(name="doctor")(_doctor_cmd)
app.command(name="reindex-fts")(_reindex_fts_cmd)

__all__ = ["app"]
