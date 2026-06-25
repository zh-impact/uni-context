// Package pdf provides adapters for the port.PDFExtractor interface.
// Three engines are supported: gxpdf (pure Go, default), shell
// (subprocess such as pdftotext), and http (POST binary to a service).
package pdf

import (
	"context"
	"fmt"
	"io"
	"strings"

	"github.com/coregx/gxpdf"

	"uni-context/internal/port"
)

// GxpdfExtractor wraps github.com/coregx/gxpdf. Pure Go, no external
// deps; the default engine when none is configured.
type GxpdfExtractor struct {
	log io.Writer
}

// NewGxpdfExtractor constructs an extractor that logs non-fatal
// warnings (currently unused by gxpdf's API — page.ExtractText swallows
// per-page errors internally and returns "" — but retained for parity
// with sibling adapters and future-proofing). Pass io.Discard in
// production if you want silent operation, or a *bytes.Buffer in tests
// to assert on warnings.
func NewGxpdfExtractor(log io.Writer) *GxpdfExtractor {
	return &GxpdfExtractor{log: log}
}

// Compile-time interface check.
var _ port.PDFExtractor = (*GxpdfExtractor)(nil)

// Extract reads content as a PDF and returns the concatenated text of
// all pages. Pages are joined with "\n". Per-page extraction errors are
// swallowed by gxpdf (it logs via slog and returns "" for that page);
// the remaining pages still contribute their text. Encrypted PDFs (no
// password supplied) return an error containing "encrypted".
//
// The provided ctx is forwarded to gxpdf's OpenFromBytesWithContext so
// opening respects cancellation/timeout. Once the document is open,
// extraction itself is CPU-bound and not cancellable per-page.
func (x *GxpdfExtractor) Extract(ctx context.Context, content []byte) (string, error) {
	doc, err := gxpdf.OpenFromBytesWithContext(ctx, content)
	if err != nil {
		// Distinguish encrypted-PDF errors from generic parse errors so
		// callers can surface "password required" specifically. gxpdf
		// wraps the parser's "encrypted PDF:" error through %w chains,
		// so a substring match on the final message is the most robust
		// heuristic without coupling to internal error types.
		if isEncryptedErr(err) {
			return "", fmt.Errorf("encrypted pdf: password required: %w", err)
		}
		return "", fmt.Errorf("open pdf: %w", err)
	}
	defer doc.Close()

	pages := doc.Pages()
	var b strings.Builder
	for i, page := range pages {
		// Respect ctx between pages; once a page's ExtractText starts
		// it runs to completion (gxpdf does not accept a ctx there).
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		default:
		}
		// gxpdf's Page.ExtractText returns only string — internal
		// errors are logged via slog and surface as "". We append
		// whatever it returns (possibly empty) so blank pages simply
		// contribute nothing.
		b.WriteString(page.ExtractText())
		if i < len(pages)-1 {
			b.WriteString("\n")
		}
	}
	return b.String(), nil
}

// isEncryptedErr heuristically detects gxpdf's encryption error.
//
// The library's parser returns an error whose wrapped message chain
// contains the literal "encrypted PDF:" (see gxpdf internal/parser/
// reader.go: openWithPassword -> initDecryption). The public exported
// sentinel gxpdf.ErrEncrypted is never actually used outside errors.go,
// so errors.Is(err, gxpdf.ErrEncrypted) does NOT match the runtime
// error. Substring matching on the lowercased message is the most
// stable contract gxpdf currently offers; if a future gxpdf version
// threads the sentinel through the parser, prefer errors.Is instead.
func isEncryptedErr(err error) bool {
	return err != nil && strings.Contains(strings.ToLower(err.Error()), "encrypt")
}
