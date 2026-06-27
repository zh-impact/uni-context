"""Shell-based PDF extractor — pdftotext, mutool, etc.

Behavior-port of Go's `internal/adapter/pdf/shell.go`. Reads PDF bytes
on stdin, captures the extracted text on stdout, returns it as a string.

Error mapping matches Go:
  - Empty command → PDFExtractionFailed("empty shell command")
  - Binary missing / not executable → PDFCommandNotFound
  - Timeout → PDFExtractionFailed("shell command timeout after Ns")
  - Non-zero exit → PDFExtractionFailed with exit code + stderr snippet
    (capped at 256 chars, matching Go's snippet cap)
  - Other subprocess errors → PDFExtractionFailed

Go uses `exec.CommandContext` with ctx-cancel → DeadlineExceeded;
Python uses `subprocess.run(..., timeout=...)` → TimeoutExpired. The
underlying mechanism differs (OS signal vs thread-join) but the
observable contract is identical: bounded runtime, classified failure.

No ctx param: Python is sync. Go's ctx forwards to CommandContext for
cancellation; subprocess.run's timeout covers the bounded-runtime case.
"""

from __future__ import annotations

import subprocess

from unictx.pdf.errors import PDFCommandNotFound, PDFExtractionFailed
from unictx.pdf.extractor import PDFExtractor

__all__ = ["ShellEngine"]

_DEFAULT_TIMEOUT = 30.0
_STDERR_SNIPPET = 256


class ShellEngine:
    """Shell-out PDFExtractor — pipes bytes to an external command.

    Stateless: construct freely. Each extract() call spawns and reaps
    its own subprocess. The command is split on whitespace runs (matching
    Go's `strings.Fields`) — no shell interpretation. Use a single
    absolute path with optional CLI flags; quoting and pipelines are
    not supported.
    """

    def __init__(self, command: str, timeout: float | None = None) -> None:
        self._command = command
        # Go: `if timeout <= 0 { timeout = 30s }` — zero/negative = default.
        if timeout and timeout > 0:
            self._timeout = timeout
        else:
            self._timeout = _DEFAULT_TIMEOUT

    def extract(self, content: bytes) -> str:
        """Run the configured command, feed bytes on stdin, return stdout.

        Returns:
            Decoded stdout as str. May be empty — empty extraction is
            NOT an error, matching FitzEngine's contract.

        Raises:
            PDFExtractionFailed: Empty command, timeout, non-zero exit,
                or any other subprocess failure.
            PDFCommandNotFound: Binary missing or not executable.
        """
        if not self._command or not self._command.strip():
            raise PDFExtractionFailed("empty shell command")

        # strings.Fields equivalent — split on whitespace runs.
        argv = self._command.split()
        try:
            completed = subprocess.run(
                argv,
                input=content,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            # Binary not on PATH — Go equivalent: cmd.ProcessState == nil
            # because the process was never started.
            raise PDFCommandNotFound(self._command) from exc
        except PermissionError as exc:
            # File exists but isn't executable — same user-facing remedy
            # (chmod +x or pick a different command), so classify the same.
            raise PDFCommandNotFound(self._command) from exc
        except subprocess.TimeoutExpired as exc:
            raise PDFExtractionFailed(
                f"shell command timeout after {self._timeout}s"
            ) from exc
        except OSError as exc:
            raise PDFExtractionFailed(reason=str(exc)) from exc

        if completed.returncode != 0:
            stderr_text = completed.stderr.decode("utf-8", errors="replace")
            snippet = stderr_text[:_STDERR_SNIPPET]
            raise PDFExtractionFailed(
                reason=(
                    f"shell command exited {completed.returncode}: {snippet}"
                )
            )

        return completed.stdout.decode("utf-8", errors="replace")


# Compile-time-ish Protocol check (Python's runtime_checkable only
# verifies method names, but this catches accidental signature drift
# at import time if the Protocol's method set changes).
_IS_PROTOCOL: PDFExtractor = ShellEngine(command="cat")  # type: ignore[assignment]
del _IS_PROTOCOL
