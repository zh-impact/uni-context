"""Tests for ShellEngine — subprocess-backed PDF extractor.

Real subprocesses are spawned. Tests rely on universally-available
POSIX utilities (cat, false, sleep, sh) and skip on Windows. The
shell engine is intended for pdftotext/mutool; here we use cat as a
stand-in to exercise the plumbing without depending on poppler.

Error categories exercised:
  - Happy path (cat echoes stdin)
  - Empty / whitespace command
  - Command not found (FileNotFoundError → PDFCommandNotFound)
  - Non-zero exit (false / helper script)
  - Stderr snippet cap (256 chars)
  - Timeout (sleep with 0.2s budget)
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from unictx.pdf.errors import PDFCommandNotFound, PDFExtractionFailed
from unictx.pdf.extractor import PDFExtractor
from unictx.pdf.shell_engine import ShellEngine

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="shell engine targets POSIX utilities (cat, false, sh)",
)


def _has(name: str) -> bool:
    return shutil.which(name) is not None


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_shell_engine_satisfies_protocol() -> None:
    assert isinstance(ShellEngine(command="cat"), PDFExtractor)


# ---------------------------------------------------------------------------
# Default timeout
# ---------------------------------------------------------------------------


def test_default_timeout_is_30s() -> None:
    assert ShellEngine(command="cat")._timeout == 30.0


def test_explicit_timeout_overrides_default() -> None:
    assert ShellEngine(command="cat", timeout=5)._timeout == 5.0


def test_zero_timeout_falls_back_to_default() -> None:
    """Go: `if timeout <= 0 { timeout = 30s }` — zero/negative = default."""
    assert ShellEngine(command="cat", timeout=0)._timeout == 30.0
    assert ShellEngine(command="cat", timeout=-1)._timeout == 30.0


# ---------------------------------------------------------------------------
# Empty command — surfaces at extract() time, not constructor time
# ---------------------------------------------------------------------------


def test_empty_command_raises_extraction_failed() -> None:
    with pytest.raises(PDFExtractionFailed):
        ShellEngine(command="").extract(b"")


def test_whitespace_command_raises_extraction_failed() -> None:
    with pytest.raises(PDFExtractionFailed):
        ShellEngine(command="   ").extract(b"")


# ---------------------------------------------------------------------------
# Happy path — `cat` echoes stdin verbatim
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has("cat"), reason="cat not on PATH")
def test_cat_round_trips_pdf_bytes() -> None:
    content = b"not really a pdf but cat doesn't care"
    assert ShellEngine(command="cat").extract(content) == content.decode()


@pytest.mark.skipif(not _has("cat"), reason="cat not on PATH")
def test_extract_returns_str_not_bytes() -> None:
    assert isinstance(ShellEngine(command="cat").extract(b"hi"), str)


@pytest.mark.skipif(not _has("cat"), reason="cat not on PATH")
def test_extract_empty_bytes_returns_empty_string() -> None:
    assert ShellEngine(command="cat").extract(b"") == ""


# ---------------------------------------------------------------------------
# Command not found
# ---------------------------------------------------------------------------


def test_missing_binary_raises_command_not_found() -> None:
    with pytest.raises(PDFCommandNotFound):
        ShellEngine(command="definitely-not-on-path-xyz123").extract(b"")


def test_command_not_found_carries_command_attribute() -> None:
    with pytest.raises(PDFCommandNotFound) as exc_info:
        ShellEngine(command="no-such-binary-xyz").extract(b"")
    assert exc_info.value.command == "no-such-binary-xyz"


# ---------------------------------------------------------------------------
# Non-zero exit
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has("false"), reason="false not on PATH")
def test_nonzero_exit_raises_extraction_failed() -> None:
    """`false` exits 1 with empty stderr."""
    with pytest.raises(PDFExtractionFailed):
        ShellEngine(command="false").extract(b"")


@pytest.mark.skipif(not _has("false"), reason="false not on PATH")
def test_nonzero_exit_message_contains_returncode() -> None:
    with pytest.raises(PDFExtractionFailed) as exc_info:
        ShellEngine(command="false").extract(b"")
    assert "1" in str(exc_info.value)


def test_nonzero_exit_includes_stderr_snippet(tmp_path: Path) -> None:
    """A helper script writes 'boom' to stderr and exits 2."""
    script = tmp_path / "fail_with_stderr.py"
    script.write_text(
        "#!/usr/bin/env python\n"
        "import sys\n"
        "sys.stderr.write('boom')\n"
        "sys.exit(2)\n"
    )
    script.chmod(0o755)
    with pytest.raises(PDFExtractionFailed) as exc_info:
        ShellEngine(command=str(script)).extract(b"")
    assert "boom" in str(exc_info.value)
    assert "2" in str(exc_info.value)


def test_stderr_snippet_truncated(tmp_path: Path) -> None:
    """stderr beyond the snippet cap is dropped (matches Go's 256-char cap)."""
    script = tmp_path / "fail_with_long_stderr.py"
    script.write_text(
        "#!/usr/bin/env python\n"
        "import sys\n"
        "sys.stderr.write('x' * 1000)\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)
    with pytest.raises(PDFExtractionFailed) as exc_info:
        ShellEngine(command=str(script)).extract(b"")
    x_count = str(exc_info.value).count("x")
    assert x_count < 1000, f"stderr not truncated: got {x_count} x chars"


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has("sleep"), reason="sleep not on PATH")
def test_timeout_raises_extraction_failed() -> None:
    """sleep 5 with 0.2s timeout — should time out."""
    engine = ShellEngine(command="sleep 5", timeout=0.2)
    with pytest.raises(PDFExtractionFailed) as exc_info:
        engine.extract(b"")
    assert "timeout" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has("sleep"), reason="sleep not on PATH")
def test_command_with_arg_runs_correctly() -> None:
    """`sleep 0.1` — split into ['sleep', '0.1'], runs successfully."""
    assert ShellEngine(command="sleep 0.1", timeout=2).extract(b"") == ""
