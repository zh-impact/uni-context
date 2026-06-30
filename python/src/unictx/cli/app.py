"""Typer app skeleton + wire() container factory + global flags.

Plan §Task 6.1. Three concerns live here:

1. **``app`` (Typer instance)** — the root CLI. Global flags ``--config``,
   ``--json``, ``--verbose`` are parsed by ``@app.callback()`` and stored
   in module-level globals (``_config_path`` / ``_json_mode`` / ``_verbose``).
   Subcommand files (Tasks 6.2-6.5) read them via ``is_json_mode()`` /
   ``get_config_path()`` / ``get_verbose()`` accessors.

2. **``wire(cfg)``** — pure factory. Opens the DB, runs migrations, and
   composes every service from concrete impls. **The only file in cli/
   that imports ``storage/*_impl.py`` directly** — guard test
   (tests/cli/test_no_direct_storage_import.py) enforces this. Everything
   else goes through service Protocols.

3. **``AppContainer``** — the wire-time dataclass that holds services.
   Mirrors Go's ``*app.App``. The CLI's subcommands access services via
   this container (loaded once per invocation via ``wire(load_config())``).

Deferred to later phases:
  - (none currently; Plan 2c self-heal landed — see :func:`_reconcile_model`.)
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import typer

from unictx.config import Config, EmbedderConfig
from unictx.embed.backfill import BackfillService
from unictx.embed.diagnostic import DiagnosticService
from unictx.embed.embedder import Embedder, ModelInfo
from unictx.embed.errors import ModelConflict, ModelNotFound
from unictx.embed.model_registry import ModelSpec
from unictx.embed.model_service import ModelService
from unictx.embed.reembed import ReembedService
from unictx.embed.service import EmbedService
from unictx.embed.worker import WorkerService
from unictx.items.access_service import AccessService
from unictx.items.ingest import IngestService
from unictx.items.item_service import ItemService
from unictx.items.reindex_fts import ReindexFTSService
from unictx.pdf.factory import build_pdf_extractor
from unictx.search.service import SearchService
from unictx.storage.access_repo_impl import AccessRepoImpl
from unictx.storage.db import open_db
from unictx.storage.embedding_repo_impl import EmbeddingRepoImpl
from unictx.storage.filestore import FileStoreImpl
from unictx.storage.migrations_runner import migrate
from unictx.storage.model_registry_impl import ModelDescriptor, ModelRegistryImpl
from unictx.storage.repo_impl import ContextRepoImpl
from unictx.storage.schema_meta import SchemaMetaImpl
from unictx.storage.search_adapter import CompositeSearcher
from unictx.storage.searcher_impl import SearcherImpl
from unictx.storage.vectorstore_impl import VectorStoreImpl

__all__ = [
    "AppContainer",
    "app",
    "get_config_path",
    "get_verbose",
    "is_json_mode",
    "reset_flags",
    "wire",
]


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AppContainer:
    """Wire-time container — holds all services + the live DB connection.

    Mirrors Go's ``*app.App``. Nullable fields (``embed``, ``models``,
    ``backfill``, ``worker``, ``reembed``) are ``None`` when the embedder
    is disabled (Plan 1). All other services are always constructed.

    Subcommand code reads services off the container the CLI loaded once
    per invocation via ``wire(unictx.config.load(get_config_path()))``.
    """

    config: Config
    db: sqlite3.Connection
    ingest: IngestService
    items: ItemService
    search: SearchService
    reindex_fts: ReindexFTSService
    diagnostics: DiagnosticService
    # P1 access-direction: read-side grant lookup. Exposed on the
    # container so a future grant-management CLI can share it; for now
    # only SearchService consumes it for scope convergence.
    access: AccessRepoImpl
    # P1.1 access management: application-layer service backing the
    # ``access grant add|list|remove`` CLI commands.
    access_svc: AccessService
    # Embed-pipeline services. None when embedder is disabled.
    embed: EmbedService | None
    models: ModelService | None
    backfill: BackfillService | None
    worker: WorkerService | None
    reembed: ReembedService | None

    def close(self) -> None:
        """Close the DB connection. Idempotent — safe to call multiple times."""
        # sqlite3.Connection.close() is itself idempotent, but we guard
        # against double-close anyway to keep the contract explicit.
        with contextlib.suppress(sqlite3.ProgrammingError):
            self.db.close()


# ---------------------------------------------------------------------------
# wire() — pure factory
# ---------------------------------------------------------------------------


def _reconcile_model(
    registry: ModelRegistryImpl,
    emb_cfg: EmbedderConfig,
    *,
    log: IO[str] | None = None,
) -> None:
    """Plan 2c self-heal: ensure the cfg-driven model is registered.

    When ``embedder.enabled=True``, the user expects the model named in
    ``emb_cfg.model`` to be usable without first running ``embed model add``.
    Reconcile makes that work:

    1. If a row with ``slug == emb_cfg.model`` already exists → no-op.
    2. Otherwise register one from the cfg fields. A lost INSERT race
       (another caller registered between our pre-check and INSERT) is
       swallowed; the existing row wins.
    3. After (2), if no default exists anywhere, set the cfg model as
       default. **Does not** override an existing default — user intent
       from a prior ``embed switch`` is preserved. Use ``embed switch``
       to change defaults.

    Idempotent: re-running with the same cfg is a no-op once the model
    exists. Safe to call on every wire().
    """
    if not emb_cfg.model:
        return  # defensive; apply_defaults fills model when enabled

    try:
        registry.get(emb_cfg.model)
        return  # already registered
    except ModelNotFound:
        pass

    spec = ModelSpec(
        slug=emb_cfg.model,
        provider=emb_cfg.provider,
        base_url=emb_cfg.base_url,
        api_key=emb_cfg.api_key,
        dimension=emb_cfg.dimension,
    )
    try:
        registry.register(spec)
    except ModelConflict:
        # Race: another wire() beat us to it. The existing row wins.
        if log is not None:
            with contextlib.suppress(Exception):
                print(
                    f"reconcile: lost race registering {emb_cfg.model!r}; "
                    "using existing row",
                    file=log,
                )
        return

    # If no default exists anywhere, set the cfg model as default.
    try:
        registry.get_active()
    except ModelNotFound:
        registry.set_default(emb_cfg.model)


def wire(cfg: Config, *, log: IO[str] | None = None) -> AppContainer:
    """Build the AppContainer from a Config.

    Composes concrete impls (``storage/*_impl.py``) into services. The
    sole legitimate boundary for storage imports in cli/ — see
    tests/cli/test_no_direct_storage_import.py.

    Behavior:
      - mkdir-p's ``cfg.data_dir``.
      - Opens ``<data_dir>/unictx.db`` (read-write; Phase 8 may add a
        read-only mode for parity verification).
      - Runs all migrations.
      - Constructs all storage impls (ContextRepo, Searcher, VectorStore,
        EmbeddingRepo, FileStore, ModelRegistry, SchemaMeta).
      - Builds a PDF extractor unconditionally — ``build_pdf_extractor``
        returns ``None`` when PDF is unconfigured, and ``IngestService``
        errors only if a PDF is actually passed.
      - If ``cfg.embedder.enabled``: runs :func:`_reconcile_model`
        (Plan 2c self-heal) so the cfg-driven model is registered, then
        resolves the active model. Reconcile is idempotent and never
        overrides a user-chosen default.
    """
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = cfg.data_dir / "unictx.db"
    db = open_db(db_path)
    try:
        migrate(db)

        # Storage impls (one per Protocol).
        repo = ContextRepoImpl(db)
        fts_searcher = SearcherImpl(db)
        vector_store = VectorStoreImpl(db)
        # CompositeSearcher satisfies the Phase 1 Searcher Protocol by
        # composing FTS (SearcherImpl) + vector (VectorStoreImpl) into
        # one object. Phase 2 split the two; Phase 1 + Phase 5 expect a
        # single Searcher with both methods. See search_adapter.py.
        searcher = CompositeSearcher(fts_searcher, vector_store)
        embedding_repo = EmbeddingRepoImpl(db)
        fs = FileStoreImpl(cfg.data_dir / "filestore")
        registry = ModelRegistryImpl(db)
        schema_meta = SchemaMetaImpl(db)
        # P1 access direction: read-side grant lookup, shared with
        # SearchService for scope convergence.
        access_repo = AccessRepoImpl(db)

        # PDF extractor — None when PDF is unconfigured.
        pdf_extractor = build_pdf_extractor(cfg.pdf)

        # Embedder + embed-pipeline services. None when disabled.
        embedder: Embedder | None = None
        embed_svc: EmbedService | None = None
        models: ModelService | None = None
        backfill: BackfillService | None = None
        worker: WorkerService | None = None
        reembed: ReembedService | None = None

        if cfg.embedder.enabled:
            # Plan 2c self-heal: register the cfg-driven model if it's
            # missing. Idempotent; never overrides a user-chosen default.
            _reconcile_model(registry, cfg.embedder, log=log)
            active = registry.get_active()
            embedder = _build_embedder_from_active(cfg.embedder, active)

            embed_svc = EmbedService(embedder, vector_store, repo, fs, embedding_repo, log=log)
            models = ModelService(registry, embedding_repo)
            backfill = BackfillService(repo, embed_svc, log=log)
            worker = WorkerService(repo, embedding_repo, embed_svc, log=log)
            reembed = ReembedService(
                repo,
                embed_svc,
                ModelInfo(slug=active.slug, dimension=active.dimension),
                log=log,
            )

        # Services constructed unconditionally — they exist in every plan.
        ingest = IngestService(repo, fs, log=log, embed=embed_svc, pdf_extractor=pdf_extractor)
        items = ItemService(repo, fs)
        search = SearchService(
            searcher, repo, log=log, embedder=embedder, access_repo=access_repo
        )
        reindex_fts = ReindexFTSService(repo, fs, log=log)
        diagnostics = DiagnosticService(schema_meta, embedder=embedder)
        # P1.1 access management service — always constructed (grants
        # matter in every plan). Wraps access_repo so the CLI never
        # imports storage impls directly.
        access_svc = AccessService(access_repo)

        return AppContainer(
            config=cfg,
            db=db,
            ingest=ingest,
            items=items,
            search=search,
            reindex_fts=reindex_fts,
            diagnostics=diagnostics,
            access=access_repo,
            access_svc=access_svc,
            embed=embed_svc,
            models=models,
            backfill=backfill,
            worker=worker,
            reembed=reembed,
        )
    except Exception:
        db.close()
        raise


def _build_embedder_from_active(
    cfg: EmbedderConfig,
    active: ModelDescriptor,
) -> Embedder:
    """Pick the embedder class based on the active model's provider.

    Mirrors Go's switch at app.go:143-154. ``ollama`` → OllamaEmbedder;
    ``openai`` (a.k.a. ``openai-compat`` in cfg) → OpenAIEmbedder. An
    unknown provider is a hard error.

    Note: ``cfg`` is unused on the success path — the active row is the
    source of truth post-reconcile. Kept in the signature for parity
    with Go's Wire signature and to leave room for reconcile in a later
    task (which would consume cfg.Embedder fields directly).
    """
    _ = cfg  # parity seam; see docstring
    if active.provider == "ollama":
        from unictx.embed.ollama import OllamaEmbedder

        return OllamaEmbedder(
            base_url=active.base_url,
            model=active.slug,
            dimension=active.dimension,
        )
    if active.provider in ("openai", "openai-compat"):
        # cfg uses "openai-compat" as the canonical key (see EmbedderConfig
        # binding-parity note); DB rows historically use "openai". Accept
        # both so reconcile-registered rows (which inherit cfg's provider
        # spelling) build the same Embedder as manually-registered ones.
        from unictx.embed.openai import OpenAIEmbedder

        return OpenAIEmbedder(
            base_url=active.base_url,
            model=active.slug,
            dimension=active.dimension,
            api_key=active.api_key,
        )
    raise ValueError(f"unsupported provider {active.provider!r} for active model {active.slug!r}")


# ---------------------------------------------------------------------------
# Typer app + global flags
# ---------------------------------------------------------------------------


app = typer.Typer(
    name="unictx",
    help="Personal context management — notes, PDFs, and search with hybrid retrieval.",
    no_args_is_help=True,
    add_completion=False,
)


# Module-level flag state. Set by the callback below; read by subcommand
# files via the accessor functions. Tests reset via reset_flags().
_config_path: Path | None = None
_json_mode: bool = False
_verbose: bool = False


def reset_flags() -> None:
    """Reset module-level flag globals to defaults.

    Used by tests (autouse fixture) so flag state from one test does not
    leak into the next. Production code calls this only implicitly — each
    CLI invocation is a fresh process.
    """
    global _config_path, _json_mode, _verbose
    _config_path = None
    _json_mode = False
    _verbose = False


def get_config_path() -> Path | None:
    """``--config`` value or None when unset (loads XDG default in that case)."""
    return _config_path


def is_json_mode() -> bool:
    """``--json`` value. Subcommands use this to switch output formatting."""
    return _json_mode


def get_verbose() -> bool:
    """``--verbose`` value. When True, subcommands emit extra diagnostic output."""
    return _verbose


@app.callback()
def _main(  # noqa: PLR0913 - Typer translates these to CLI flags
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Path to config.yaml (default: $XDG_CONFIG_HOME/unictx/config.yaml).",
    ),
    json: bool = typer.Option(  # noqa: A002 - mirrors --json flag name
        False,
        "--json",
        help="Emit machine-readable JSON instead of human-friendly tables.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Emit extra diagnostic output (SQL timing, HTTP requests, etc.).",
    ),
) -> None:
    """uni-context — personal context management.

    Global flags parsed here are stashed in module globals; subcommand
    files (Tasks 6.2-6.5) read them via ``is_json_mode()`` /
    ``get_config_path()`` / ``get_verbose()``.

    Each invocation of the CLI is a fresh process, so these globals are
    single-threaded by construction — no need for locking.
    """
    global _config_path, _json_mode, _verbose
    _config_path = config
    _json_mode = json
    _verbose = verbose
