"""Shared fixtures for CLI tests.

The module-level flag globals in :mod:`unictx.cli.app` (``_json_mode``,
``_config_path``, ``_verbose``) are set by the root app's callback and
read by subcommands. Without a reset between tests, a test that sets
``--json`` (e.g. ``test_search_json_output``) leaks ``_json_mode=True``
into neighbouring tests that assert plain-text output (e.g.
``test_add_positional_creates_note``).

The autouse fixture below runs before + after every CLI test so flag
state from one test never bleeds into the next. It originally lived in
``test_app.py``; promoted to ``conftest.py`` here so it applies across
all CLI test modules, not just ``test_app``.
"""

from __future__ import annotations

import pytest

from unictx.cli.app import reset_flags


@pytest.fixture(autouse=True)
def _reset_cli_flags_around_each_test() -> None:
    """Reset module-level flag globals before + after each CLI test."""
    reset_flags()
    yield
    reset_flags()
