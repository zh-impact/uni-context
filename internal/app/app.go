package app

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"

	"uni-context/internal/adapter/embedder/ollama"
	"uni-context/internal/adapter/embedder/openai"
	"uni-context/internal/adapter/fsstore"
	"uni-context/internal/adapter/sqlite"
	"uni-context/internal/config"
	"uni-context/internal/domain"
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
	Backfill *service.BackfillService
	// Worker is populated when the embedder is enabled; nil otherwise.
	// Plan 2b Task 6: long-running retry loop for status='failed' rows.
	Worker *service.WorkerService

	// Reembed is populated when the embedder is enabled; nil otherwise.
	// Plan 2c: bulk re-embed items under the active model after `embed switch`.
	Reembed *service.ReembedService

	// Registry is non-nil when cfg.Embedder.Enabled is true. CLI uses it
	// for `embed model add/list/remove` and `embed switch`. Plan 2c.
	Registry port.ModelRegistry

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
	// worker is nil unless embedder is enabled. Plan 2b Task 6: long-running
	// retry loop for status='failed' rows driven by 'embed worker'.
	var worker *service.WorkerService
	// reembed is nil unless embedder is enabled. Plan 2c: bulk re-embeds
	// items lacking a done row for the active model (post `embed switch`).
	var reembed *service.ReembedService
	// registry is nil unless embedder is enabled. Plan 2c Task 4: exposes
	// the ModelRegistry so the CLI's `embed model add/list/remove` can
	// reach it without re-deriving a handle from the DB.
	var registry port.ModelRegistry
	if cfg.Embedder.Enabled {
		registry = sqlite.NewModelRegistry(db)

		// First-Plan-2c-run reconciliation. After this, DB is authoritative
		// and `embed switch` is the only way to change the active model.
		if err := reconcilePlan2cSync(context.Background(), db, registry, cfg.Embedder); err != nil {
			_ = db.Close()
			return nil, fmt.Errorf("plan 2c reconcile: %w", err)
		}

		active, err := registry.GetActive(context.Background())
		if err != nil {
			_ = db.Close()
			return nil, fmt.Errorf("read active model: %w", err)
		}

		// Construct embedder for the active model's provider + config.
		switch active.Provider {
		case "ollama":
			embedder = ollama.New(active.BaseURL, active.Slug, active.Dimension)
		case "openai":
			// OpenAI-compat: LMStudio (local, no key), OpenAI hosted
			// (key required), vLLM, etc. apiKey empty = no auth header.
			embedder = openai.New(active.BaseURL, active.Slug, active.Dimension, active.APIKey)
		default:
			_ = db.Close()
			return nil, fmt.Errorf("unsupported provider %q for active model %q",
				active.Provider, active.Slug)
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
		// Plan 2b Task 6: WorkerService retries status='failed' rows by
		// calling EmbedService.Embed per item; shares repo so it can
		// hydrate externalized content via FileStore.
		worker = service.NewWorkerService(repo, embeddingRepo, embedSvc)
		// Plan 2c: ReembedService targets items without a done row for the
		// active model so `embed switch` + `embed reembed` migrates them.
		reembed = service.NewReembedService(repo, embedSvc, port.ModelInfo{
			Slug: active.Slug, Dimension: active.Dimension,
		})
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
		Worker:        worker,
		Reembed:       reembed,
		Registry:      registry,
		Ingest:        ingest,
		Search:        search,
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

// reconcilePlan2cSync runs once on first Plan 2c Wire invocation, gated by
// schema_meta.plan_2c_synced. After first run, DB is authoritative and
// cfg.Embedder (except `enabled`) is ignored — `embed switch` becomes the
// only way to change the active model.
//
// Behavior:
//  1. If plan_2c_synced == '1', return immediately.
//  2. If cfg.Embedder.Model not in DB: Register from cfg.Embedder fields.
//     If exists: UpdateConfig to overwrite provider + config JSON with
//     cfg.Embedder values (heals Plan 2b alias rows whose config was '{}').
//  3. SetDefault(cfg.Embedder.Model) — atomic flip; idempotent if already default.
//  4. INSERT OR REPLACE schema_meta plan_2c_synced = '1'.
func reconcilePlan2cSync(ctx context.Context, db *sql.DB, reg port.ModelRegistry, cfg config.EmbedderConfig) error {
	var synced string
	err := db.QueryRowContext(ctx,
		`SELECT value FROM schema_meta WHERE key = 'plan_2c_synced'`).Scan(&synced)
	if err == nil && synced == "1" {
		return nil
	}
	if err != nil && err != sql.ErrNoRows {
		return fmt.Errorf("read plan_2c_synced flag: %w", err)
	}

	_, getErr := reg.Get(ctx, cfg.Model)
	switch {
	case getErr == nil:
		// Row exists: heal config from cfg.Embedder.
		if err := reg.UpdateConfig(ctx, cfg.Model, cfg.BaseURL, cfg.APIKey, cfg.Provider); err != nil {
			return fmt.Errorf("heal config for %s: %w", cfg.Model, err)
		}
	case errors.Is(getErr, domain.ErrNotFound):
		// Row missing: register fresh.
		if err := reg.Register(ctx, port.ModelSpec{
			Slug: cfg.Model, Provider: cfg.Provider,
			BaseURL: cfg.BaseURL, APIKey: cfg.APIKey, Dimension: cfg.Dimension,
		}); err != nil {
			return fmt.Errorf("register %s: %w", cfg.Model, err)
		}
	default:
		return fmt.Errorf("lookup %s: %w", cfg.Model, getErr)
	}

	if err := reg.SetDefault(ctx, cfg.Model); err != nil {
		return fmt.Errorf("set default %s: %w", cfg.Model, err)
	}

	if _, err := db.ExecContext(ctx,
		`INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('plan_2c_synced', '1')`); err != nil {
		return fmt.Errorf("set plan_2c_synced flag: %w", err)
	}
	return nil
}
