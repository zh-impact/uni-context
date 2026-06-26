package port

import "context"

// PDFExtractor extracts plain text from a PDF document.
//
// Empty extraction (image-only/scanned PDF, no text layer) returns
// ("", nil) — NOT an error. Callers decide how to handle empty text
// per their UX; the user-note-add flow stores the PDF blob with empty
// Content in this case.
//
// Actual failures (malformed PDF, encrypted, IO error, downstream
// HTTP 5xx) return ("", err). Callers SHOULD surface these to the user.
type PDFExtractor interface {
	Extract(ctx context.Context, content []byte) (text string, err error)
}
