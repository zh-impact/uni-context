package pdf

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"uni-context/internal/port"
)

// HttpExtractor POSTs the PDF bytes to a configured URL and reads
// the response body as plain text. Expected response Content-Type is
// text/plain (any charset); other MIMEs return an error.
type HttpExtractor struct {
	url       string
	timeout   time.Duration
	authToken string
}

// NewHttpExtractor constructs an HTTP-based extractor. If timeout <= 0
// it defaults to 30s. authToken is optional; when empty, no
// Authorization header is sent.
func NewHttpExtractor(url string, timeout time.Duration, authToken string) *HttpExtractor {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return &HttpExtractor{url: url, timeout: timeout, authToken: authToken}
}

// Compile-time interface check.
var _ port.PDFExtractor = (*HttpExtractor)(nil)

// Extract POSTs content to the configured URL with Content-Type
// application/pdf and an optional Bearer token, then reads the response
// body as the extracted text.
//
// Error categories:
//   - timeout: ctx hits DeadlineExceeded before the request completes.
//   - non-2xx: status code plus a body snippet truncated to 256 bytes.
//   - wrong response MIME: response Content-Type does not start with
//     text/plain.
func (x *HttpExtractor) Extract(ctx context.Context, content []byte) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, x.timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, x.url, bytes.NewReader(content))
	if err != nil {
		return "", fmt.Errorf("build http request: %w", err)
	}
	req.Header.Set("Content-Type", "application/pdf")
	if x.authToken != "" {
		req.Header.Set("Authorization", "Bearer "+x.authToken)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return "", fmt.Errorf("http request timeout after %s: %w", x.timeout, ctx.Err())
		}
		return "", fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// Body snippet bounded to 256 bytes: error responses can be
		// arbitrarily large HTML/JSON payloads and we only want enough
		// to surface the failure reason to the user.
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
		return "", fmt.Errorf("http %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	ct := resp.Header.Get("Content-Type")
	if !strings.HasPrefix(strings.ToLower(ct), "text/plain") {
		return "", fmt.Errorf("unexpected response MIME %q, want text/plain", ct)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read response: %w", err)
	}
	return string(body), nil
}
