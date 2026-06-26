import re
from pathlib import Path

# Combined pattern to catch:
# 1. `from unictx.storage... import` (original)
# 2. `import unictx.storage` (plain module import)
# 3. `import unictx.storage.repo_impl` (submodule import)
# 4. `from unictx import storage` (importing storage module)
FORBIDDEN_PATTERNS = [
    re.compile(r"^\s*from\s+unictx\.storage(?:\.[a-z_]+_impl)?\s+import", re.MULTILINE),
    re.compile(r"^\s*import\s+unictx\.storage(\.|\s|$)", re.MULTILINE),
    re.compile(r"^\s*from\s+unictx\s+import\s+.*\bstorage\b", re.MULTILINE),
]


def test_cli_does_not_import_storage_impls():
    """CLI must go through services, never storage/*_impl.py directly."""
    cli_dir = Path(__file__).parent.parent.parent / "src" / "unictx" / "cli"
    assert cli_dir.is_dir(), f"cli dir missing: {cli_dir}"
    offenders = []
    for py_file in cli_dir.rglob("*.py"):
        text = py_file.read_text()
        for pattern in FORBIDDEN_PATTERNS:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(
                    f"{py_file.relative_to(cli_dir)}:{line_no}: {match.group().strip()}"
                )
    assert not offenders, (
        "CLI files must not import from unictx.storage or unictx.storage.*_impl "
        "(go through services instead). Offenders:\n  " + "\n  ".join(offenders)
    )
