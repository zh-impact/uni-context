package app

import (
	"database/sql"
	"fmt"
	"os"

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

	Ingest *service.IngestService
	Search *service.SearchService
}

// Wire opens the DB (running migrations), builds adapters and services,
// and returns a fully assembled App. Caller is responsible for calling
// App.Close when finished.
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

	return &App{
		Config:   cfg,
		DB:       db,
		Repo:     repo,
		Project:  proj,
		Searcher: searcher,
		FS:       fs,
		Ingest:   service.NewIngestService(repo, fs),
		Search:   service.NewSearchService(searcher, repo),
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
