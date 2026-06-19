package app

import (
	"database/sql"
	"fmt"
	"os"

	"uni-context/internal/adapter/embedder/ollama"
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

	// Embedder + EmbedService wiring (Plan 2a). Only constructed when
	// the user explicitly opts in via embedder.enabled.
	var embedder port.Embedder
	var embedSvc *service.EmbedService
	if cfg.Embedder.Enabled {
		switch cfg.Embedder.Provider {
		case "ollama":
			embedder = ollama.New(cfg.Embedder.BaseURL, cfg.Embedder.Model, cfg.Embedder.Dimension)
		default:
			_ = db.Close()
			return nil, fmt.Errorf("unsupported embedder provider: %s", cfg.Embedder.Provider)
		}
		vectorStore := sqlite.NewVectorStore(db)
		embedSvc = service.NewEmbedService(embedder, vectorStore, repo)
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
		Config:   cfg,
		DB:       db,
		Repo:     repo,
		Project:  proj,
		Searcher: searcher,
		FS:       fs,
		Embedder: embedder,
		Ingest:   ingest,
		Search:   search,
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
