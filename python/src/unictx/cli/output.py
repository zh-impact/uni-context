"""Single-purpose JSON output helper.

Mirrors Go's ``printJSON(v)`` (output.go). Each subcommand does::

    if json_mode:
        print_json(result)
    else:
        <rich table or plain print>

This is deliberately NOT a branching ``format_result`` — non-JSON output
is rendered per-command with rich tables (Tasks 6.2-6.5). Keeping the
helper focused avoids the Go-archive anti-pattern of one giant switch
on result type.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer

__all__ = ["print_json"]


def _default(obj: Any) -> str:
    """Fallback serializer for leaves ``dataclasses.asdict`` doesn't reach.

    Order matters: ``Path`` check first because ``Path`` instances don't
    carry ``__dataclass_fields__`` and aren't iterable as dataclass leaves.
    """
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "isoformat"):  # datetime / date / time
        return obj.isoformat()
    raise TypeError(f"not serializable: {type(obj)}")


def _to_serializable(obj: Any) -> Any:
    """Recursively turn dataclasses into dicts so json.dumps can encode them.

    Handles single dataclass instances, lists/tuples of dataclasses, and
    nested combinations. Non-dataclass values pass through unchanged
    (json.dumps + _default handle Path/datetime leaves).
    """
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_to_serializable(item) for item in obj]
    return obj


def print_json(result: Any) -> None:
    """Print ``result`` as indented JSON to stdout.

    Handles:

    - ``@dataclass(slots=True)`` instances (ContextItem, SearchHit, ...) via
      :func:`dataclasses.asdict`.
    - ``list``/``tuple`` of dataclasses (each element converted).
    - ``Path`` leaves → ``str``.
    - ``datetime`` leaves → ``isoformat()``.
    - Plain ``dict`` / ``list`` passthrough (no dataclass conversion).
    - Non-serializable leaves → ``TypeError`` from ``_default``.

    Domain models are ``@dataclass(slots=True)`` (not Pydantic), so
    Pydantic's serializer doesn't apply.
    """
    typer.echo(json.dumps(_to_serializable(result), default=_default, indent=2))
