package service

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"uni-context/internal/adapter/embedder/fake"
	"uni-context/internal/domain"
	"uni-context/internal/port"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestIngest_Create_SmallContentInline(t *testing.T) {
	f := newIngestFixture(t)
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Title:       "Test",
		Content:     "small content",
		Tags:        []string{"t1"},
	})
	require.NoError(t, err)
	assert.NotEmpty(t, id)

	got, err := f.repo.Get(context.Background(), id)
	require.NoError(t, err)
	assert.Equal(t, "Test", got.Title)
	assert.Equal(t, "small content", got.Content)
	assert.Empty(t, got.ContentURI)
	assert.Equal(t, []string{"t1"}, got.Tags)
	assert.Greater(t, got.WordCount, 0)
}

func TestIngest_Create_LargeContentExternalized(t *testing.T) {
	f := newIngestFixture(t)
	large := strings.Repeat("word ", 1000) // ~5KB
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     large,
	})
	require.NoError(t, err)

	got, _ := f.repo.Get(context.Background(), id)
	assert.Empty(t, got.Content, "inline content should be emptied")
	assert.NotEmpty(t, got.ContentURI, "content_uri should be set")
	assert.Contains(t, got.ContentURI, "file://")
	assert.NotEmpty(t, got.ContentHash)

	// FileStore can resolve the content
	data, err := f.fs.Get(got.ContentURI)
	require.NoError(t, err)
	assert.Equal(t, large, string(data))
}

func TestIngest_Create_RejectsInvalidScope(t *testing.T) {
	f := newIngestFixture(t)
	_, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeGlobal, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1", // invalid with global
	})
	require.Error(t, err)
	assert.ErrorIs(t, err, domain.ErrValidation)
}

func TestIngest_Create_DeduplicatesByContentHash(t *testing.T) {
	f := newIngestFixture(t)
	content := strings.Repeat("a", 5000)
	id1, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     content,
	})
	require.NoError(t, err)

	id2, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     content,
	})
	require.NoError(t, err)

	// Two items, same hash, single filestore entry
	assert.NotEqual(t, id1, id2)
	got1, _ := f.repo.Get(context.Background(), id1)
	got2, _ := f.repo.Get(context.Background(), id2)
	assert.Equal(t, got1.ContentHash, got2.ContentHash)
	assert.Equal(t, got1.ContentURI, got2.ContentURI)
}

// TestIngest_Create_RollsBackFileStoreOnRepoFailure locks in I2: when
// large content has been externalized via fs.Put but repo.Create then
// fails, the service must call fs.Delete to drop the refcount back to 0
// (removing the file). Otherwise the filestore accumulates orphaned
// refcount=1 entries that nothing references — a leak that becomes a
// correctness problem in Plan 2 where the same flow also writes
// embeddings.
func TestIngest_Create_RollsBackFileStoreOnRepoFailure(t *testing.T) {
	f := newIngestFixture(t)
	large := strings.Repeat("a", 5000) // exceeds ContentInlineLimit (4KB)

	// Force repo.Create to fail on the next call.
	f.repo.createErr = fmt.Errorf("simulated persistence failure")

	_, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     large,
	})
	require.Error(t, err, "Create should propagate the repo error")

	// fsstore layout: <root>/<hex[:2]>/<hex> + <hex>.meta. After Put +
	// Delete (refcount 1→0), both files are removed. The fixture's fsRoot
	// starts empty (t.TempDir), so any leftover file = orphan = rollback
	// failed.
	var orphans []string
	err = filepath.WalkDir(f.fsRoot, func(path string, d os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if d.IsDir() {
			return nil
		}
		// fsRoot itself is empty dir; bucket dirs are fine if empty.
		orphans = append(orphans, path)
		return nil
	})
	require.NoError(t, err)
	assert.Empty(t, orphans,
		"filestore should be empty after rollback; found orphaned files: %v", orphans)
}

// TestIngest_Create_TriggersEmbed_WhenConfigured verifies that when an
// EmbedService is wired in via NewIngestServiceWithEmbedder, Create
// synchronously writes a vector and flips any_embedding=1. This is the
// happy path of Plan 2a's synchronous embed path.
func TestIngest_Create_TriggersEmbed_WhenConfigured(t *testing.T) {
	vs, repo, db := newMemVectorStore(t)
	defer db.Close()
	emb := fake.New("fake-model", 8)
	embedSvc := NewEmbedService(emb, vs, repo, newMemFileStore(t), newMemEmbeddingRepo(t, db), io.Discard)
	svc := NewIngestServiceWithEmbedder(repo, newMemFileStore(t), embedSvc, io.Discard)

	ctx := context.Background()
	id, err := svc.Create(ctx, Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Title:       "deploy",
		Content:     "small",
	})
	require.NoError(t, err)

	// any_embedding flipped to 1 by the embed path
	got, _ := repo.Get(ctx, id)
	assert.Equal(t, 1, got.AnyEmbedding, "Create with embedder should set any_embedding=1")

	// Vector is searchable: query with the fake's embedding of the
	// same composed text the service fed in (title + "\n\n" + content).
	vecs, _ := emb.Embed(ctx, []string{"deploy\n\nsmall"})
	hits, err := vs.Search(ctx, port.VectorQuery{
		Vector: vecs[0], Model: "fake-model", Limit: 5,
	})
	require.NoError(t, err)
	require.Len(t, hits, 1)
	assert.Equal(t, id, hits[0].ID)
}

// TestIngest_Create_SucceedsWhenEmbedFails locks in the error-tolerance
// contract: a broken embedder must NOT fail Create. The item is still
// persisted and FTS-searchable; any_embedding stays 0.
func TestIngest_Create_SucceedsWhenEmbedFails(t *testing.T) {
	vs, repo, db := newMemVectorStore(t)
	defer db.Close()
	emb := &failingEmbedder{}
	embedSvc := NewEmbedService(emb, vs, repo, newMemFileStore(t), newMemEmbeddingRepo(t, db), io.Discard)
	svc := NewIngestServiceWithEmbedder(repo, newMemFileStore(t), embedSvc, io.Discard)

	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     "x",
	})
	require.NoError(t, err, "Create must succeed even if embed fails")
	require.NotEmpty(t, id)

	got, _ := repo.Get(context.Background(), id)
	assert.Equal(t, 0, got.AnyEmbedding, "any_embedding stays 0 on embed failure")
}

// failingEmbedder is a port.Embedder that always errors. Used to verify
// IngestService.Create tolerates embed failures.
type failingEmbedder struct{}

func (failingEmbedder) Model() port.ModelInfo { return port.ModelInfo{Slug: "fail", Dimension: 1} }
func (failingEmbedder) Embed(context.Context, []string) ([][]float32, error) {
	return nil, fmt.Errorf("simulated embedder failure")
}

// TestIngest_Create_LargeContentWithMIMEExternalizesToFS verifies that
// when content exceeds ContentInlineLimit and MIME is set, the FileStore
// receives the correct MIME (stored in .meta) and the item carries it on
// ContentMIME. This is the path a large .md file import takes.
func TestIngest_Create_LargeContentWithMIMEExternalizesToFS(t *testing.T) {
	f := newIngestFixture(t)
	large := strings.Repeat("word ", 1000) // ~5KB > 4KB ContentInlineLimit
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     large,
		MIME:        "text/markdown",
	})
	require.NoError(t, err)

	got, err := f.repo.Get(context.Background(), id)
	require.NoError(t, err)
	assert.Empty(t, got.Content, "inline content should be emptied")
	assert.NotEmpty(t, got.ContentURI, "content_uri should be set")
	assert.Equal(t, "text/markdown", got.ContentMIME,
		"ContentMIME must reflect the caller-specified MIME for externalized content")

	// FileStore .meta must carry the MIME so re-embed / hydration knows the type.
	data, err := f.fs.Get(got.ContentURI)
	require.NoError(t, err)
	assert.Equal(t, large, string(data))
}

// TestIngest_Create_DefaultMIMEIsTextPlainWhenEmpty verifies that when
// MIME is empty (existing callers: inline text, stdin), the externalize
// path falls back to text/plain — preserving Plan 1 behavior byte-for-byte.
func TestIngest_Create_DefaultMIMEIsTextPlainWhenEmpty(t *testing.T) {
	f := newIngestFixture(t)
	large := strings.Repeat("a", 5000) // > 4KB
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     large,
		// MIME intentionally omitted
	})
	require.NoError(t, err)

	got, _ := f.repo.Get(context.Background(), id)
	assert.Equal(t, "text/plain", got.ContentMIME,
		"empty MIME must default to text/plain on the externalize path")
}

// TestIngest_Create_SmallContentPreservesMIMEInline verifies that a small
// file import (< ContentInlineLimit) with MIME set preserves the MIME on
// item.ContentMIME even though the content stays inline (not in FileStore).
// This is the key invariant for .md file imports: the MIME survives on the
// item so downstream renderers know it's markdown without consulting FileStore.
func TestIngest_Create_SmallContentPreservesMIMEInline(t *testing.T) {
	f := newIngestFixture(t)
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     "# tiny markdown",
		MIME:        "text/markdown",
	})
	require.NoError(t, err)

	got, err := f.repo.Get(context.Background(), id)
	require.NoError(t, err)
	assert.NotEmpty(t, got.Content, "small content stays inline")
	assert.Empty(t, got.ContentURI, "small content is not externalized")
	assert.Equal(t, "text/markdown", got.ContentMIME,
		"MIME must be preserved on inline items when caller sets it")
}

// TestIngest_Create_EmptyMIMELeavesContentMIMEEmptyInline is a regression
// guard: existing callers (inline text, stdin) pass MIME="". The inline
// path must NOT set ContentMIME in that case, preserving the Plan 1
// invariant where inline items have ContentMIME="".
func TestIngest_Create_EmptyMIMELeavesContentMIMEEmptyInline(t *testing.T) {
	f := newIngestFixture(t)
	id, err := f.svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u-1",
		Content:     "small content",
		// MIME intentionally omitted
	})
	require.NoError(t, err)

	got, _ := f.repo.Get(context.Background(), id)
	assert.Empty(t, got.ContentMIME,
		"existing callers with MIME='' must leave ContentMIME empty on inline items")
}

// TestIngest_Create_ExternalizedContentIsFTSSearchable is the service-level
// integration test for the externalized-content FTS fix. The bug: when
// content exceeds ContentInlineLimit (4KB) it is externalized to FileStore,
// leaving item.Content="" — but the AFTER INSERT trigger on context_item
// reads new.content when writing the FTS row, so the FTS index captured ""
// and `search "keyword"` returned 0 hits even when the keyword was in the
// file. The fix: IngestService.Create calls repo.ReindexFTS after a
// successful Create, rewriting the FTS row with the hydrated content.
//
// This test uses the real SQLite-backed repo + searcher (not fakeRepo) so
// the FTS5 trigger + ReindexFTS actually run end-to-end.
func TestIngest_Create_ExternalizedContentIsFTSSearchable(t *testing.T) {
	f := newSearchFixture(t)
	ctx := context.Background()

	// Build content > 4KB containing a unique searchable needle.
	// Repeating the needle ensures we exceed ContentInlineLimit while
	// keeping the test deterministic.
	needle := "uniquefindabletoken"
	large := strings.Repeat("filler filler filler ", 250) + needle // ~5KB

	id, err := f.ingest.Create(ctx, Input{
		Scope:       domain.ScopeUser,
		Kind:        domain.KindNote,
		Source:      domain.SourceManual,
		OwnerUserID: "u-1",
		Title:       "Externalized Note",
		Content:     large,
	})
	require.NoError(t, err)
	require.NotEmpty(t, id)

	// The fix: search must find the item by the needle. Without
	// ReindexFTS, the AFTER INSERT trigger captured "" and this returns 0.
	resp, err := f.svc.Search(ctx, SearchRequest{Query: needle, Limit: 10})
	require.NoError(t, err)
	require.Len(t, resp.Results, 1, "externalized content must be FTS-searchable post-ReindexFTS")
	assert.Equal(t, id, resp.Results[0].Item.ID)
}

// TestIngestService_Create_PDF_ErrorsWithoutExtractor locks in the
// "PDF not configured" guard: when MIME=application/pdf arrives and no
// extractor was supplied (neither constructor nor per-call), Create must
// return a clear actionable error pointing at pdf.engine / --engine
// rather than silently persisting raw PDF bytes as if they were text.
func TestIngestService_Create_PDF_ErrorsWithoutExtractor(t *testing.T) {
	f := newIngestFixture(t)
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf) // no WithPDFExtractor

	_, err := svc.Create(context.Background(), Input{
		Scope:       domain.ScopeUser,
		Kind:        domain.KindNote,
		Source:      domain.SourceManual,
		OwnerUserID: "u1",
		Title:       "paper",
		Content:     "%PDF-1.4 fake",
		MIME:        "application/pdf",
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "pdf extraction not configured")
	assert.Contains(t, err.Error(), "pdf.engine")
}

// fakePDFExtractor is a port.PDFExtractor double for service tests.
// Records the bytes it was called with so tests can assert override
// behavior (WithExtractor must receive the raw PDF bytes, not extracted
// text). Returns the configured text + err on each call.
type fakePDFExtractor struct {
	called   bool
	gotBytes []byte
	text     string
	err      error
}

func (f *fakePDFExtractor) Extract(_ context.Context, content []byte) (string, error) {
	f.called = true
	f.gotBytes = content
	return f.text, f.err
}

// TestIngestService_Create_PDF_ExtractsAndStoresBlob is the happy path:
// text is extracted, raw PDF bytes are stored in FileStore, and the
// resulting item carries the extracted text + the original_uri pointer.
func TestIngestService_Create_PDF_ExtractsAndStoresBlob(t *testing.T) {
	f := newIngestFixture(t)
	ext := &fakePDFExtractor{text: "extracted body text"}
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf, WithPDFExtractor(ext))

	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Title: "paper",
		Content: "%PDF-1.4 fake", MIME: "application/pdf",
	})
	require.NoError(t, err)
	require.NotEmpty(t, id)
	require.True(t, ext.called, "extractor must be called")
	assert.Equal(t, "%PDF-1.4 fake", string(ext.gotBytes),
		"extractor receives the raw PDF bytes")

	item, err := f.repo.Get(context.Background(), id)
	require.NoError(t, err)
	assert.Equal(t, "extracted body text", item.Content,
		"Content is the extracted text")
	assert.Equal(t, "text/plain", item.ContentMIME,
		"MIME rewired to text/plain post-extraction")
	pdfURI, ok := item.SourceMeta["original_uri"].(string)
	require.True(t, ok, "SourceMeta.original_uri must be a string")
	assert.NotEmpty(t, pdfURI, "SourceMeta.original_uri must be set")
	assert.Equal(t, "application/pdf", item.SourceMeta["original_mime"])
}

// TestIngestService_Create_PDF_EmptyExtraction_StoresBlobEmptyContent
// locks in the image-only-PDF contract: extracted text is "" but the
// blob is still stored, AND the embed path is skipped (no title-only
// vectors for an unreadable-as-text body).
//
// Uses a real SQLite-backed repo+vs so the embedder's vector write
// would actually land if the skip logic didn't fire. This makes the test
// positively assert "no vector written" rather than just "log line
// present" — much stronger.
func TestIngestService_Create_PDF_EmptyExtraction_StoresBlobEmptyContent(t *testing.T) {
	vs, repo, db := newMemVectorStore(t)
	defer db.Close()
	emb := fake.New("fake-model", 8)
	embedSvc := NewEmbedService(emb, vs, repo, newMemFileStore(t), newMemEmbeddingRepo(t, db), io.Discard)

	// Capture log so we can assert on the skip warning.
	var logBuf bytes.Buffer
	ext := &fakePDFExtractor{text: ""} // image-only PDF
	ingestFS := newMemFileStore(t)
	svc := NewIngestServiceWithEmbedder(repo, ingestFS, embedSvc, &logBuf, WithPDFExtractor(ext))

	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Title: "image-only paper",
		Content: "%PDF-1.4 fake", MIME: "application/pdf",
	})
	require.NoError(t, err)
	require.NotEmpty(t, id)

	item, err := repo.Get(context.Background(), id)
	require.NoError(t, err)
	assert.Empty(t, item.Content, "Content is empty for image-only PDF")
	pdfURI, ok := item.SourceMeta["original_uri"].(string)
	require.True(t, ok)
	assert.NotEmpty(t, pdfURI, "PDF blob URI still captured")

	logStr := logBuf.String()
	assert.Contains(t, logStr, "pdf extraction yielded no text")
	assert.Contains(t, logStr, "skipping embed")

	// Stronger assertion: no vector was actually written. If the skip
	// logic missed (e.g. only checked item.Content without the pdfURI
	// scope), the constructor-default embed would fire and a vector for
	// the title would land in vs.
	vecs, _ := emb.Embed(context.Background(), []string{"image-only paper\n\n"})
	hits, err := vs.Search(context.Background(), port.VectorQuery{
		Vector: vecs[0], Model: "fake-model", Limit: 10,
	})
	require.NoError(t, err)
	assert.Empty(t, hits, "no vector should be written for image-only PDF")
}

// TestIngestService_Create_PDF_PropagatesExtractorError verifies that
// extraction failures (encrypted, malformed, IO, downstream 5xx) are
// wrapped and returned — not silently swallowed into an empty-content
// item. Empty extraction is a different path (returns "", nil).
func TestIngestService_Create_PDF_PropagatesExtractorError(t *testing.T) {
	f := newIngestFixture(t)
	ext := &fakePDFExtractor{err: errors.New("encrypted pdf: password required")}
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf, WithPDFExtractor(ext))

	_, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Content: "fake", MIME: "application/pdf",
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "extract pdf")
	assert.Contains(t, err.Error(), "encrypted pdf")
}

// TestIngestService_Create_PDF_WithExtractorOverride verifies per-call
// override semantics: even when the constructor did NOT wire an
// extractor (PDF disabled by default), the CLI can supply one for this
// invocation via WithExtractor. The constructor default for next call
// remains unaffected (not tested here — constructor state is immutable
// after NewIngestService returns).
func TestIngestService_Create_PDF_WithExtractorOverride(t *testing.T) {
	// Constructor default is nil (PDF not configured); per-call
	// override via WithExtractor supplies the extractor for this call.
	f := newIngestFixture(t)
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf) // no WithPDFExtractor

	ext := &fakePDFExtractor{text: "from override"}
	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Content: "fake", MIME: "application/pdf",
	}, WithExtractor(ext))
	require.NoError(t, err)
	require.NotEmpty(t, id)
	require.True(t, ext.called, "override extractor must be called")

	item, _ := f.repo.Get(context.Background(), id)
	assert.Equal(t, "from override", item.Content)
}

// TestIngestService_Create_PDF_LargeExtractedText_ExternalizesTextOnly
// verifies the rewiring carries through the externalize path: extracted
// text > 4KB triggers fs.Put (text URI on item.ContentURI), while the
// PDF blob URI lives separately on SourceMeta.original_uri. The two
// URIs must be distinct — conflating them would mean FTS hydration
// pulls PDF bytes instead of text.
func TestIngestService_Create_PDF_LargeExtractedText_ExternalizesTextOnly(t *testing.T) {
	f := newIngestFixture(t)
	// Build extracted text > 4KB so existing externalization fires.
	big := strings.Repeat("a", 5000)
	ext := &fakePDFExtractor{text: big}
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf, WithPDFExtractor(ext))

	id, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Content: "%PDF fake", MIME: "application/pdf",
	})
	require.NoError(t, err)

	item, err := f.repo.Get(context.Background(), id)
	require.NoError(t, err)
	require.NotEmpty(t, item.ContentURI, "extracted text externalized → ContentURI set")
	assert.Empty(t, item.Content, "Content is empty when externalized")

	pdfURI, _ := item.SourceMeta["original_uri"].(string)
	require.NotEmpty(t, pdfURI, "PDF blob URI captured in SourceMeta")
	assert.NotEqual(t, item.ContentURI, pdfURI,
		"text URI and PDF URI must be distinct")
}

// TestIngestService_Create_PDF_RollsBackBothBlobsOnRepoFailure mirrors
// the existing TestIngest_Create_RollsBackFileStoreOnRepoFailure
// pattern: force repo.Create to fail, then walk fsRoot to confirm NO
// orphan files remain. In the PDF path with large extracted text, TWO
// fs.Put calls happen before repo.Create (PDF blob + externalized
// text), so rollback must fs.Delete BOTH. If only one is cleaned up,
// fsRoot will contain leftover files and the test fails.
func TestIngestService_Create_PDF_RollsBackBothBlobsOnRepoFailure(t *testing.T) {
	f := newIngestFixture(t)
	// Force repo.Create to fail. fakeRepo.createErr is the unexported
	// failure-injection point; tests are in the same package so they
	// can set it directly (no setter method exists).
	f.repo.createErr = errors.New("simulated DB outage")

	// Extracted text > 4KB so externalization fires BEFORE the failing
	// repo.Create. This means at the point repo.Create runs, fs has TWO
	// blobs: the PDF blob (Put in PDF branch) and the text blob (Put in
	// externalize step). Both must be rolled back.
	big := strings.Repeat("a", 5000)
	ext := &fakePDFExtractor{text: big}
	var logBuf bytes.Buffer
	svc := NewIngestService(f.repo, f.fs, &logBuf, WithPDFExtractor(ext))

	_, err := svc.Create(context.Background(), Input{
		Scope: domain.ScopeUser, Kind: domain.KindNote, Source: domain.SourceManual,
		OwnerUserID: "u1", Content: "%PDF fake", MIME: "application/pdf",
	})
	require.Error(t, err)

	// fsRoot must be empty: both blobs deleted by the extended rollback.
	// Pattern lifted from TestIngest_Create_RollsBackFileStoreOnRepoFailure.
	var orphans []string
	walkErr := filepath.WalkDir(f.fsRoot, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		orphans = append(orphans, path)
		return nil
	})
	require.NoError(t, walkErr)
	assert.Empty(t, orphans,
		"both PDF blob and text blob must be rolled back; found orphaned files: %v", orphans)
}
