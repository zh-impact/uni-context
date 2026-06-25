package pdf

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
	"time"

	"uni-context/internal/port"
)

// ShellExtractor spawns an external command, writes the PDF bytes to
// its stdin, and reads extracted text from stdout. The expected usage
// is a command like `pdftotext - -` (stdin → stdout).
//
// No shell interpretation: command is split via strings.Fields, so
// pipes, redirects, and globs do NOT work. Users who need them wrap
// their pipeline in a script and point command at the script path.
type ShellExtractor struct {
	command string
	timeout time.Duration
}

// NewShellExtractor constructs an extractor that runs command per
// call. If timeout is zero, the factory layer (BuildExtractorForEngine)
// replaces it with 30s before calling this constructor — but
// defensive code in Extract also clamps zero to 30s.
func NewShellExtractor(command string, timeout time.Duration) *ShellExtractor {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return &ShellExtractor{command: command, timeout: timeout}
}

// Compile-time interface check.
var _ port.PDFExtractor = (*ShellExtractor)(nil)

// Extract runs the configured command, pipes content to its stdin, and
// returns the stdout as extracted text. The command is killed if it
// runs longer than the configured timeout.
//
// Error categories:
//   - timeout: ctx hits DeadlineExceeded before the process exits.
//   - not found / not executable: cmd.ProcessState == nil after Run
//     (binary missing, not executable, permission denied).
//   - non-zero exit: includes the exit code and a stderr snippet.
func (x *ShellExtractor) Extract(ctx context.Context, content []byte) (string, error) {
	parts := strings.Fields(x.command)
	if len(parts) == 0 {
		return "", fmt.Errorf("shell extractor: empty command")
	}
	cmdName := parts[0]
	args := parts[1:]

	ctx, cancel := context.WithTimeout(ctx, x.timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, cmdName, args...)
	cmd.Stdin = bytes.NewReader(content)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		// Distinguish timeout from generic exit-non-zero.
		if ctx.Err() == context.DeadlineExceeded {
			return "", fmt.Errorf("shell command timeout after %s: %w",
				x.timeout, ctx.Err())
		}
		// Process never started: binary not found, not executable, or
		// permission denied. exec.CommandContext returns *os.PathError
		// for absolute paths that don't exist and *exec.Error for PATH
		// misses — checking either type explicitly misses the other.
		// cmd.ProcessState == nil is the reliable signal: if the process
		// never started, there's no ProcessState to read.
		if cmd.ProcessState == nil {
			return "", fmt.Errorf("shell command not found or not executable: %s: %w", cmdName, err)
		}
		// Exit non-zero: include exit code + stderr snippet.
		stderrSnippet := strings.TrimSpace(stderr.String())
		if len(stderrSnippet) > 256 {
			stderrSnippet = stderrSnippet[:256] + "..."
		}
		return "", fmt.Errorf("shell command failed (exit %v): %s",
			cmd.ProcessState.ExitCode(), stderrSnippet)
	}
	return stdout.String(), nil
}
