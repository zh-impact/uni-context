"""Tests for cli.output.print_json — single-purpose JSON helper.

Mirrors Go's printJSON(v) (output.go). Each subcommand does:
``if json_mode: print_json(result); else: <rich table>``.

print_json handles:
  - @dataclass(slots=True) (ContextItem, SearchHit, ...) via asdict.
  - Path leaves → str.
  - datetime leaves → isoformat.
  - Plain dict / list passthrough.
  - Non-serializable leaves → TypeError (stdlib json default).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unictx.cli.output import print_json
from unictx.items.models import ContextItem, Kind, Scope, Source


def test_print_json_serializes_dataclass_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """ContextItem (dataclass) → JSON with snake_case field names."""
    item = ContextItem(
        id="i-1",
        scope=Scope.USER,
        kind=Kind.NOTE,
        source=Source.MANUAL,
        owner_user_id="u-1",
        title="hello",
    )
    print_json(item)
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["id"] == "i-1"
    assert parsed["scope"] == "user"
    assert parsed["kind"] == "note"
    assert parsed["title"] == "hello"
    assert parsed["owner_user_id"] == "u-1"
    # All 25 ContextItem fields present.
    assert len(parsed) == 25, "every ContextItem field serializes"


def test_print_json_passes_plain_dict_through(capsys: pytest.CaptureFixture[str]) -> None:
    """Non-dataclass dict is serialized as-is."""
    print_json({"hello": "world", "n": 42})
    out = capsys.readouterr().out
    assert json.loads(out) == {"hello": "world", "n": 42}


def test_print_json_serializes_list_of_dataclasses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """List[ContextItem] → JSON array of dicts."""
    a = ContextItem(id="a", title="A")
    b = ContextItem(id="b", title="B")
    print_json([a, b])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[0]["id"] == "a"
    assert parsed[1]["id"] == "b"


def test_print_json_handles_path_leaf(capsys: pytest.CaptureFixture[str]) -> None:
    """Path values → str. ContextItem doesn't carry Path, so wrap in a dict."""
    print_json({"data_dir": Path("/tmp/data")})
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["data_dir"] == "/tmp/data"


def test_print_json_handles_datetime_leaf(capsys: pytest.CaptureFixture[str]) -> None:
    """datetime values → isoformat string."""
    ts = datetime(2026, 6, 27, 12, 30, 0, tzinfo=UTC)
    print_json({"created_at": ts})
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["created_at"] == "2026-06-27T12:30:00+00:00"


def test_print_json_raises_on_non_serializable_leaf() -> None:
    """Truly unserializable leaves raise TypeError (delegates to stdlib json).

    Uses a custom object with no __dataclass_fields__, no __iter__, no
    isoformat, not a Path. The _default fallback exhausts every branch.
    """

    class _Opaque:
        pass

    with pytest.raises(TypeError, match=r"not serializable"):
        print_json({"thing": _Opaque()})


def test_print_json_indents_with_two_spaces(capsys: pytest.CaptureFixture[str]) -> None:
    """Output is human-readable: indent=2, one field per line."""
    print_json({"a": 1, "b": 2})
    out = capsys.readouterr().out
    # 2-space indent means each key sits on its own line preceded by "  ".
    assert '\n  "a":' in out
    assert '\n  "b":' in out
