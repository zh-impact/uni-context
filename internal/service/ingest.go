package service

import (
	"context"
	"fmt"
	"io"
	"strings"
	"unicode"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

type IngestService struct {
	repo  port.ContextRepo
	fs    port.FileStore
	embed *EmbedService // nil = embedding disabled (Plan 1 compat)
	// log receives non-fatal warnings (reindex-fts failure, embed failure).
	// Injected via constructor so tests can assert on warnings and the
	// service has no direct os.Stderr coupling.
	log io.Writer

	// pdfExtractor enables PDF→text extraction on Create when set. nil =
	// PDF support disabled: an Input with MIME "application/pdf" returns
	// a clear actionable error pointing the caller at pdf.engine / --engine.
	// Configured via the WithPDFExtractor constructor option (Task 7
	// constructs the adapter and wires it here).
	pdfExtractor port.PDFExtractor
}

// IngestOption configures an IngestService at construction time.
type IngestOption func(*IngestService)

// WithPDFExtractor is the constructor-time option that enables PDF→text
// extraction. Without it (and without a per-call WithExtractor override),
// passing Input with MIME "application/pdf" returns a clear error.
//
// Distinct from the per-call WithExtractor (a CreateOption) — Go has no
// overloading, so the two option types carry different names even though
// they accept the same argument shape.
func WithPDFExtractor(ext port.PDFExtractor) IngestOption {
	return func(s *IngestService) { s.pdfExtractor = ext }
}

// CreateOption configures a single Create call. Applied AFTER the
// constructor options; overrides constructor defaults for this call only.
// Used by the CLI to pass --engine per-invocation without rebuilding the
// service.
type CreateOption func(*createConfig)

type createConfig struct {
	extractor port.PDFExtractor
}

// WithExtractor is the per-call override for the PDF extractor. Used when
// the CLI passes --engine to choose a different engine than the config
// default for a single ingestion.
func WithExtractor(ext port.PDFExtractor) CreateOption {
	return func(c *createConfig) { c.extractor = ext }
}

func NewIngestService(repo port.ContextRepo, fs port.FileStore, log io.Writer, opts ...IngestOption) *IngestService {
	s := &IngestService{repo: repo, fs: fs, log: log}
	for _, opt := range opts {
		opt(s)
	}
	return s
}

// NewIngestServiceWithEmbedder wires an optional EmbedService. If embed
// is nil, behavior is identical to NewIngestService (Plan 1: no vector
// writes). When non-nil, Create embeds synchronously after a successful
// repo.Create; embed failure is non-fatal (warned to log, item is
// still returned and FTS-searchable).
func NewIngestServiceWithEmbedder(repo port.ContextRepo, fs port.FileStore, embed *EmbedService, log io.Writer, opts ...IngestOption) *IngestService {
	s := &IngestService{repo: repo, fs: fs, embed: embed, log: log}
	for _, opt := range opts {
		opt(s)
	}
	return s
}

// Input is the user-facing write request.
type Input struct {
	Scope       domain.Scope
	Kind        domain.Kind
	Source      domain.Source
	OwnerUserID string
	ProjectID   string
	AgentID     string

	Title   string
	Summary string
	Content string
	Tags    []string

	SourceMeta map[string]any

	// MIME of the content. Empty means "treat as text/plain" (preserves
	// existing behavior for inline/stdin notes — no caller update required).
	// Set by the CLI when importing a .md file so FileStore's .meta and
	// item.ContentMIME both carry the right MIME. For inline-sized file
	// imports (< ContentInlineLimit), MIME is still written to item.ContentMIME
	// so downstream renderers can tell a .md import from a .txt import even
	// when the bytes didn't go through FileStore.
	MIME string
}

func (s *IngestService) Create(ctx context.Context, in Input, opts ...CreateOption) (string, error) {
	// Apply per-call options over constructor defaults. Per-call wins
	// because opts run after the constructor's WithPDFExtractor already
	// seeded s.pdfExtractor into cfg.extractor.
	cfg := createConfig{extractor: s.pdfExtractor}
	for _, opt := range opts {
		opt(&cfg)
	}

	// pdfURI is captured at function scope so the repo.Create rollback
	// below can clean up the PDF blob too. Empty when no PDF branch ran
	// (non-PDF inputs, or PDF inputs that errored before fs.Put).
	var pdfURI string

	// PDF branch runs BEFORE NewContextItem for two reasons:
	//   1. item.WordCount = countWords(in.Content) further down must count
	//      the EXTRACTED TEXT, not the raw PDF bytes — otherwise the word
	//      count is meaningless (binary garbage).
	//   2. The externalize step (fs.Put([]byte(in.Content), mime)) must
	//      store extracted text, not PDF bytes — otherwise the FileStore
	//      blob is binary garbage and FTS hydration returns garbage.
	// Rewriting in.Content / in.MIME here means the rest of Create is
	// PDF-unaware; it just sees a normal text Input.
	if in.MIME == "application/pdf" {
		if cfg.extractor == nil {
			return "", fmt.Errorf(
				"pdf extraction not configured: set pdf.engine in config or pass --engine")
		}
		// SourceMeta nil-guard: the existing nil-check below (line ~80)
		// protects item.SourceMeta which is built later from in.SourceMeta.
		// Here we must ensure in.SourceMeta itself is writable before the
		// PDF branch populates original_uri/original_mime. The CLI always
		// initializes the map, but a future API caller could pass nil.
		if in.SourceMeta == nil {
			in.SourceMeta = map[string]any{}
		}
		text, err := cfg.extractor.Extract(ctx, []byte(in.Content))
		if err != nil {
			return "", fmt.Errorf("extract pdf: %w", err)
		}
		// Store the ORIGINAL PDF bytes (not the extracted text — that
		// flows through the normal externalize path below). The URI is
		// captured on SourceMeta so the blob is retrievable later for
		// re-extraction, download, or preview.
		pdfURI, _, err = s.fs.Put([]byte(in.Content), "application/pdf")
		if err != nil {
			return "", fmt.Errorf("store pdf blob: %w", err)
		}
		if text == "" {
			// Image-only / scanned PDF: no text layer to extract. We
			// still persist the blob (user may want to download or
			// OCR later), but downstream embed must be skipped to avoid
			// producing a title-only vector (see skipEmbed below).
			fmt.Fprintf(s.log,
				"warning: pdf extraction yielded no text (likely image-only or scanned); "+
					"storing blob with empty content — search/embedding will not hit body text\n")
		}
		in.SourceMeta["original_uri"] = pdfURI
		in.SourceMeta["original_mime"] = "application/pdf"
		// Rewire Input so the rest of Create sees plain text. The PDF
		// bytes are already durably stored via fs.Put above; from here
		// on, the inline/externalize/repo/FTS/embed pipeline operates on
		// the extracted text exactly as it would for a plain-text note.
		in.Content = text
		in.MIME = "text/plain"
	}

	item, err := domain.NewContextItem(in.Scope, in.Kind, in.Source, domain.NewItemParams{
		OwnerUserID: in.OwnerUserID,
		ProjectID:   in.ProjectID,
		AgentID:     in.AgentID,
	})
	if err != nil {
		return "", err
	}

	item.Title = strings.TrimSpace(in.Title)
	item.Summary = in.Summary
	item.Tags = in.Tags
	if item.Tags == nil {
		item.Tags = []string{}
	}
	item.SourceMeta = in.SourceMeta
	if item.SourceMeta == nil {
		item.SourceMeta = map[string]any{}
	}
	item.WordCount = countWords(in.Content)

	mime := in.MIME
	if mime == "" {
		mime = "text/plain"
	}
	if len(in.Content) > domain.ContentInlineLimit {
		uri, hash, err := s.fs.Put([]byte(in.Content), mime)
		if err != nil {
			return "", fmt.Errorf("externalize content: %w", err)
		}
		item.ContentURI = uri
		item.ContentHash = hash
		item.ContentMIME = mime
		item.Content = ""
	} else {
		item.Content = in.Content
		if in.MIME != "" {
			item.ContentMIME = in.MIME
		}
	}

	if err := s.repo.Create(ctx, item); err != nil {
		// Roll back the filestore entries we just bumped. Without this,
		// a failed repo.Create leaves orphaned refcount=1 blobs that
		// nothing references. fs.Delete decrements refcount; when it
		// hits 0 the file is removed.
		//
		// Two possible orphans:
		//   - item.ContentURI: set when extracted text was externalized
		//     (len > ContentInlineLimit). Always rolled back since Plan 1.
		//   - pdfURI: set by the PDF branch above when storing the raw
		//     PDF blob. MUST also be rolled back, otherwise a DB write
		//     failure leaks the PDF blob forever (nothing references it).
		if item.ContentURI != "" {
			_ = s.fs.Delete(item.ContentURI)
		}
		if pdfURI != "" {
			_ = s.fs.Delete(pdfURI)
		}
		return "", fmt.Errorf("persist item: %w", err)
	}

	// Externalized-content fix: the AFTER INSERT trigger on context_item
	// wrote an FTS row reading new.content, which is "" for externalized
	// items (bytes live in FileStore). Without this rewrite the item is
	// silently unsearchable via FTS — `search "keyword"` returns 0 hits
	// even when the keyword exists in the file. ReindexFTS rewrites the
	// FTS row with the hydrated content. We still have in.Content in
	// memory here; no FileStore round-trip needed.
	//
	// Non-fatal: if ReindexFTS fails, the item is already saved and the
	// `unictx reindex-fts` CLI command can fix it later. The alternative
	// (failing the whole Create) would punish the user for a search-only
	// index bug, which doesn't match how we treat embed failures.
	if item.ContentURI != "" {
		if err := s.repo.ReindexFTS(ctx, item.ID, item.Title, item.Summary, in.Content); err != nil {
			fmt.Fprintf(s.log, "warn: reindex fts for %s: %v\n", item.ID, err)
		}
	}

	// Synchronous embed after the item is durably persisted. Embedding
	// failure is non-fatal — the item is already saved and FTS-searchable,
	// and the async worker (Plan 2b) will retry on its next iteration.
	// any_embedding stays 0 until the worker flips it; SearchService
	// treats 0 as "not vector-searchable" (correct).
	//
	// For externalized items, item.Content is "" here and EmbedService
	// hydrates from FileStore via item.ContentURI.
	//
	// Embed-skip is scoped to the image-only-PDF case (pdfURI != "" with
	// no extracted text and no externalized text URI). Without this guard,
	// an image-only PDF would still produce a title-only vector — which
	// is misleading (a downstream vector search would surface a hit for
	// a document whose body the user can't actually read as text). The
	// pdfURI != "" check is load-bearing: we must NOT change embed
	// behavior for non-PDF empty-content items (existing contract calls
	// embed unconditionally — see TestIngest_Create_TriggersEmbed_WhenConfigured).
	skipEmbed := pdfURI != "" && item.Content == "" && item.ContentURI == ""
	if s.embed != nil && !skipEmbed {
		if err := s.embed.Embed(ctx, item.ID, item.Title, item.Content); err != nil {
			fmt.Fprintf(s.log, "warn: embed failed for %s: %v\n", item.ID, err)
		}
	} else if s.embed != nil && skipEmbed {
		fmt.Fprintf(s.log, "warn: skipping embed for %s (empty extracted content)\n", item.ID)
	}
	return item.ID, nil
}

func countWords(s string) int {
	n := 0
	inWord := false
	for _, r := range s {
		if unicode.IsSpace(r) {
			inWord = false
			continue
		}
		if isCJK(r) {
			inWord = false
			n++
			continue
		}
		if !inWord {
			n++
			inWord = true
		}
	}
	return n
}

// isCJK reports whether r is a CJK ideograph or related script character
// that should be counted as one word each (no space delimiters).
func isCJK(r rune) bool {
	switch {
	case 0x4E00 <= r && r <= 0x9FFF: // CJK Unified Ideographs
		return true
	case 0x3400 <= r && r <= 0x4DBF: // CJK Extension A
		return true
	case 0x3040 <= r && r <= 0x30FF: // Hiragana + Katakana
		return true
	case 0xAC00 <= r && r <= 0xD7AF: // Hangul Syllables
		return true
	case 0xF900 <= r && r <= 0xFAFF: // CJK Compatibility Ideographs
		return true
	case 0x31C0 <= r && r <= 0x31EF: // CJK Strokes
		return true
	}
	return false
}
