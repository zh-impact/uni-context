package app

import (
	"database/sql"
	"fmt"
	"os"

	"uni-context/internal/adapter/embedder/ollama"
	"uni-context/internal/adapter/embedder/openai"
	"uni-context/internal/adapter/fsstore"
	"uni-context/internal/adapter/sqlite"
	"uni-context/internal/config"
	"uni-context/internal/port"
	"uni-context/internal/service"
)

// App is a fully wired application root: configuration plus all adapters
// and services ready for the CLI to consume.
type App struct {
	Config   *config.Config
	DB       *sql.DB
	Repo     port.ContextRepo
	Project  port.ProjectRepo
	Searcher port.Searcher
	FS       port.FileStore

	// Embedder is non-nil when cfg.Embedder.Enabled is true. When nil,
	// the app runs in Plan 1 mode (no vector indexing, search is fts-only).
	Embedder port.Embedder

	// EmbeddingRepo owns the context_embedding rows; non-nil only when
	// Embedder is constructed. Plan 2b: status rows for async backfill.
	EmbeddingRepo port.EmbeddingRepo

	// Backfill is populated when the embedder is enabled; nil otherwise
	// (Plan 1 / Plan 2a mode). Plan 2b Task 5: drives 'embed backfill'.
	// Worker (Task 6) stays any until that task tightens it.
	Backfill *service.BackfillService
	Worker   any // *service.WorkerService — Task 6

	Ingest *service.IngestService
	Search *service.SearchService
}

// Wire opens the DB (running migrations), builds adapters and services,
// and returns a fully assembled App. Caller is responsible for calling
// App.Close when finished.
//
// When cfg.Embedder.Enabled is true, Wire constructs the configured
// embedder (Ollama in Plan 2a), sqlite.VectorStore, and EmbedService,
// then injects EmbedService into IngestService (synchronous embed on
// Create) and the embedder into SearchService (enabling hybrid mode).
// When disabled, behavior is identical to Plan 1.
func Wire(cfg *config.Config) (*App, error) {
	if err := mkdirp(cfg.DataDir, cfg.FileStoreDir()); err != nil {
		return nil, err
	}
	db, err := sqlite.Open(cfg.DBPath())
	if err != nil {
		return nil, fmt.Errorf("open db: %w", err)
	}
	fs, err := fsstore.New(cfg.FileStoreDir())
	if err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("open filestore: %w", err)
	}
	repo := sqlite.NewContextRepo(db)
	proj := sqlite.NewProjectRepo(db)
	searcher := sqlite.NewSearcher(db)

	// Embedder + EmbedService wiring (Plan 2a + openai-compat preview).
	// Only constructed when the user explicitly opts in via embedder.enabled.
	var embedder port.Embedder
	var embedSvc *service.EmbedService
	// embeddingRepo is nil unless embedder is enabled. Declared at function
	// scope so the return struct can reference it without conditional returns.
	var embeddingRepo port.EmbeddingRepo
	// backfill is nil unless embedder is enabled. Plan 2b Task 5: bulk-embed
	// items where any_embedding=0 via the new 'embed backfill' CLI command.
	var backfill *service.BackfillService
	if cfg.Embedder.Enabled {
		switch cfg.Embedder.Provider {
		case "ollama":
			embedder = ollama.New(cfg.Embedder.BaseURL, cfg.Embedder.Model, cfg.Embedder.Dimension)
		case "openai":
			// OpenAI-compat: LMStudio (local, no key), OpenAI hosted
			// (key required), vLLM, etc. apiKey empty = no auth header.
			embedder = openai.New(cfg.Embedder.BaseURL, cfg.Embedder.Model, cfg.Embedder.Dimension, cfg.Embedder.APIKey)
		default:
			_ = db.Close()
			return nil, fmt.Errorf("unsupported embedder provider: %s", cfg.Embedder.Provider)
		}
		// Ensure the model slug has a row in embedding_model so VectorStore
		// can resolve it to a vec table. Plan 2a's seed only registers
		// 'bge-m3'; this lets configs use any slug (e.g. LMStudio's
		// 'text-embedding-bge-m3') at the same dimension (1024). Plan 2c
		// replaces this with true per-model vec tables.
		if err := sqlite.EnsureModelRegistered(db,
			cfg.Embedder.Model, cfg.Embedder.Provider, cfg.Embedder.Dimension); err != nil {
			_ = db.Close()
			return nil, fmt.Errorf("register embedder model: %w", err)
		}
		vectorStore := sqlite.NewVectorStore(db)
		// Plan 2b: EmbedService needs fs (hydration) + embeddingRepo (status rows).
		// embeddingRepo shares the same db so status rows and items live
		// in one DB. Exposed on App so the worker (Task 6) can reach it.
		embeddingRepo = sqlite.NewEmbeddingRepo(db)
		embedSvc = service.NewEmbedService(embedder, vectorStore, repo, fs, embeddingRepo)
		// Plan 2b Task 5: BackfillService shares repo + embedSvc so the
		// 'embed backfill' CLI can iterate unembedded items and embed each.
		backfill = service.NewBackfillService(repo, embedSvc)
	}

	ingest := service.NewIngestService(repo, fs)
	if embedSvc != nil {
		ingest = service.NewIngestServiceWithEmbedder(repo, fs, embedSvc)
	}

	search := service.NewSearchService(searcher, repo)
	if embedder != nil {
		search = service.NewSearchServiceWithEmbedder(searcher, repo, embedder)
	}

	return &App{
		Config:        cfg,
		DB:            db,
		Repo:          repo,
		Project:       proj,
		Searcher:      searcher,
		FS:            fs,
		Embedder:      embedder,
		EmbeddingRepo: embeddingRepo,
		Backfill:      backfill,
		// Worker stays nil until Task 6 populates it.
		Ingest: ingest,
		Search: search,
	}, nil
}

// Close releases resources held by the App (currently just the DB handle).
func (a *App) Close() error {
	if a.DB != nil {
		return a.DB.Close()
	}
	return nil
}

func mkdirp(dirs ...string) error {
	for _, d := range dirs {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return fmt.Errorf("mkdir %s: %w", d, err)
		}
	}
	return nil
}
