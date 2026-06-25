package app

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io"
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
	Config *config.Config

	// Infra fields are unexported: the inbound layer (CLI) must reach the
	// application through its services (Items, Ingest, Search, Diagnostics,
	// Models, ...), not by touching ports or the *sql.DB handle directly.
	// Same-package code (Wire, Close) can still read/write them.
	db       *sql.DB
	repo     port.ContextRepo
	project  port.ProjectRepo
	searcher port.Searcher
	fs       port.FileStore
	embedder port.Embedder // nil = Plan 1 (disabled)
	embRepo  port.EmbeddingRepo
	registry port.ModelRegistry

	// Backfill is populated when the embedder is enabled; nil otherwise
	// (Plan 1 / Plan 2a mode). Plan 2b Task 5: drives 'embed backfill'.
	Backfill *service.BackfillService
	// Worker is populated when the embedder is enabled; nil otherwise.
	// Plan 2b Task 6: long-running retry loop for status='failed' rows.
	Worker *service.WorkerService

	// Reembed is populated when the embedder is enabled; nil otherwise.
	// Plan 2c: bulk re-embed items under the active model after `embed switch`.
	Reembed *service.ReembedService

	// ReindexFTS is always populated. Bulk-rewrites context_fts rows for
	// items whose content was externalized, healing the AFTER INSERT
	// trigger gap that left them FTS-unsearchable. Constructed
	// unconditionally because FTS search is available in Plan 1 too.
	ReindexFTS *service.ReindexFTSService

	// Items is the query-side use case: hydrated Get + List + Delete over
	// context items. Constructed unconditionally so the CLI reads items
	// through a service rather than reaching into Repo + FS ports directly
	// (externalization-hydration policy lives here, not in the inbound layer).
	Items *service.ItemService

	// Diagnostics powers `unictx doctor`: schema version + embedder ping.
	// Constructed unconditionally so the CLI never touches *sql.DB or
	// port.Embedder directly for the doctor flow.
	Diagnostics *service.DiagnosticService

	// Models is the application-layer boundary for `embed model ...`,
	// `embed switch`, and `embed status`. Constructed only when the
	// embedder is enabled (registry + embeddingRepo exist).
	Models *service.ModelService

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
	// models is nil unless embedder is enabled. Application boundary for
	// `embed model add/list/remove`, `embed switch`, and `embed status`.
	// Constructed only when registry + embeddingRepo exist.
	var models *service.ModelService
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
		embedSvc = service.NewEmbedService(embedder, vectorStore, repo, fs, embeddingRepo, os.Stderr)
		// Plan 2b Task 5: BackfillService shares repo + embedSvc so the
		// 'embed backfill' CLI can iterate unembedded items and embed each.
		backfill = service.NewBackfillService(repo, embedSvc, os.Stderr)
		// Plan 2b Task 6: WorkerService retries status='failed' rows by
		// calling EmbedService.Embed per item; shares repo so it can
		// hydrate externalized content via FileStore.
		worker = service.NewWorkerService(repo, embeddingRepo, embedSvc, os.Stderr)
		// Plan 2c: ReembedService targets items without a done row for the
		// active model so `embed switch` + `embed reembed` migrates them.
		reembed = service.NewReembedService(repo, embedSvc, port.ModelInfo{
			Slug: active.Slug, Dimension: active.Dimension,
		}, os.Stderr)
		// ModelService is the application boundary for `embed model
		// add/list/remove`, `embed switch`, and `embed status`. Thin
		// pass-through over registry + embeddingRepo so the CLI doesn't
		// depend on those ports directly.
		models = service.NewModelService(registry, embeddingRepo)
	}

	// PDF extractor (Task: pdf-attach). Built unconditionally so a
	// misconfigured engine (e.g. shell selected but no command) fails
	// Wire loudly. BuildPDFExtractor returns (nil, nil) when PDF is
	// unconfigured — the service then errors only if a PDF is actually
	// passed, which is the right UX for users who don't use PDFs.
	pdfExt, err := BuildPDFExtractor(cfg.PDF, os.Stderr)
	if err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("build pdf extractor: %w", err)
	}
	ingestOpts := []service.IngestOption{}
	if pdfExt != nil {
		ingestOpts = append(ingestOpts, service.WithPDFExtractor(pdfExt))
	}
	ingest := service.NewIngestService(repo, fs, os.Stderr, ingestOpts...)
	if embedSvc != nil {
		ingest = service.NewIngestServiceWithEmbedder(repo, fs, embedSvc, os.Stderr, ingestOpts...)
	}

	search := service.NewSearchService(searcher, repo, os.Stderr)
	if embedder != nil {
		search = service.NewSearchServiceWithEmbedder(searcher, repo, embedder, os.Stderr)
	}

	// ReindexFTS is constructed unconditionally — the FTS gap is not
	// embedder-dependent (Plan 1 FTS-only users hit it too).
	reindexFTS := service.NewReindexFTSService(repo, fs, os.Stderr)
	// ItemService owns externalization hydration; unconditional so the CLI's
	// read/delete paths go through a use case in every plan.
	items := service.NewItemService(repo, fs)
	// DiagnosticService owns the doctor command's schema-version lookup +
	// embedder ping. Constructed unconditionally; embedder is nil when
	// Plan 1 is active, and PingEmbedder reports disabled in that case.
	diagnostics := service.NewDiagnosticService(sqlite.NewSchemaMetaRepo(db), embedder)

	return &App{
		Config:      cfg,
		db:          db,
		repo:        repo,
		project:     proj,
		searcher:    searcher,
		fs:          fs,
		embedder:    embedder,
		embRepo:     embeddingRepo,
		Backfill:    backfill,
		Worker:      worker,
		Reembed:     reembed,
		ReindexFTS:  reindexFTS,
		Items:       items,
		Diagnostics: diagnostics,
		registry:    registry,
		Models:      models,
		Ingest:      ingest,
		Search:      search,
	}, nil
}

// Close releases resources held by the App (currently just the DB handle).
// Idempotent: safe to call multiple times.
func (a *App) Close() error {
	if a.db != nil {
		err := a.db.Close()
		a.db = nil
		return err
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

// osStderr is the indirection that lets tests capture the corrupt-config
// warning without redirecting the real os.Stderr globally. Production
// code points this at os.Stderr; tests swap it to a *bytes.Buffer.
var osStderr io.Writer = os.Stderr

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
		// Row exists and scanned cleanly: heal provider + config from cfg.Embedder.
		// This is the existing Plan 2c alias-row heal; unchanged.
		if err := reg.UpdateConfig(ctx, cfg.Model, cfg.BaseURL, cfg.APIKey, cfg.Provider); err != nil {
			return fmt.Errorf("heal config for %s: %w", cfg.Model, err)
		}
	case errors.Is(getErr, sqlite.ErrCorruptConfig):
		// Row exists but config JSON is unreadable — UpdateConfig overwrites
		// the corrupt blob. Stderr warning so the user knows we touched
		// their DB; this is rare enough (manual edit / cross-version bug)
		// that a warning is appropriate rather than silent heal.
		fmt.Fprintf(osStderr,
			"warning: model %s has corrupt config JSON; healing from config.yaml\n",
			cfg.Model)
		if err := reg.UpdateConfig(ctx, cfg.Model, cfg.BaseURL, cfg.APIKey, cfg.Provider); err != nil {
			return fmt.Errorf("heal corrupt config for %s: %w", cfg.Model, err)
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
