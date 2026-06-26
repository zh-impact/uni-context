import re
from pathlib import Path

FORBIDDEN_PATTERN = re.compile(
    r"^\s*from\s+unictx\.storage(?:\.[a-z_]+_impl)?\s+import",
    re.MULTILINE,
)


def test_cli_does_not_import_storage_impls():
    """CLI must go through services, never storage/*_impl.py directly."""
    cli_dir = Path(__file__).parent.parent.parent / "src" / "unictx" / "cli"
    assert cli_dir.is_dir(), f"cli dir missing: {cli_dir}"
    offenders = []
    for py_file in cli_dir.glob("*.py"):
        text = py_file.read_text()
        for match in FORBIDDEN_PATTERN.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{py_file.name}:{line_no}: {match.group().strip()}")
    assert not offenders, (
        "CLI files must not import from unictx.storage or unictx.storage.*_impl "
        "(go through services instead). Offenders:\n  " + "\n  ".join(offenders)
    )
