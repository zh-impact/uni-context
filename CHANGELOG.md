# Changelog

Notable changes and known limitations per release. Dates are YYYY-MM-DD.

## Known Limitations

### Trigram FTS requires ≥3-character queries (affects 2-char CJK search)

The FTS5 index uses the `trigram` tokenizer for CJK-friendly matching.
Trigram indexes every contiguous 3-character sequence, so queries
shorter than 3 characters (e.g. the 2-character Chinese word `部署`)
silently return zero results — no error, just empty.

This affects:
- `unictx search <query>` where `len([]rune(query)) < 3`
- Any future caller of `SearchService.Search` with a short query

**Plan 2a update:** vector embeddings resolve this ONLY when the user
passes `--mode hybrid` (or programmatic callers select
`SearchModeHybrid`). The embedding model handles sub-word meaning
without a minimum-token rule, so hybrid search returns results for
2-char queries. Default `fts-only` mode is unchanged — short queries
still return zero. The LIKE-fallback option was considered and
rejected — LIKE on `title`/`content` would work for ASCII but scans
the whole table, and we'd be ripping it out anyway once vectors land.

Until you opt into `--mode hybrid` (see Plan 2a below), treat
sub-3-char queries as unsupported in the default search path. Search
results being empty for `部署` under `fts-only` is expected, not a bug.

## Plan 1 — Foundation (2026-06-19)

Initial release. CLI (`unictx`) for personal notes with FTS5 search,
SQLite persistence, hexagonal architecture. See
`docs/superpowers/plans/2026-06-19-foundation.md` for the plan and
`.superpowers/sdd/progress.md` for execution notes.

**Deferred to Plan 2** (from final review):
- Tags filter on search (note: Tags filter on `list` shipped in this
  patch series — see `ItemFilter.Tags`).
- 2-char CJK query support (see limitation above).
- Vector / hybrid search.

## Plan 2a — Hybrid Search (2026-06-20)

First vector-search release. Adds opt-in hybrid (FTS + vector KNN)
search on top of the Plan 1 foundation. See
`docs/superpowers/plans/2026-06-20-plan-2a-hybrid-search.md` for the
plan and `.superpowers/sdd/progress.md` for execution notes.

**What shipped:**
- **Single embedding model:** `bge-m3` via Ollama, 1024-dim, stored in
  sqlite-vec `vec_<model>` tables (migration 0002).
- **Synchronous embedding:** ingest blocks up to ~60s on Ollama per
  Create when the embedder is enabled. Failure is non-fatal — the item
  is still saved and FTS-searchable.
- **RRF hybrid search:** reciprocal-rank fusion (k=60) merges FTS5 and
  KNN top-k results with over-fetch at 3×limit.
- **`--mode hybrid` opt-in:** `unictx search --mode hybrid` (default
  remains `fts-only`). Programmatic callers pass `service.SearchModeHybrid`.
- **Error-tolerant degradation:** if the embedder is unreachable at
  search time, hybrid mode falls back to fts-only with a stderr warning
  rather than failing the query.
- **Config knob:** `embedder.enabled` (default `false`) gates all
  embedding behavior. Plan 1 users see no change until they flip it.

### Known Limitations (Plan 2a)

These are documented in code docstrings; reproduced here so a future
Plan 2b implementer can find them without grepping.

1. **Externalized-content items embed with EMPTY content.** When an
   item's content exceeds `domain.ContentInlineLimit` and is spilled to
   FileStore, only the title contributes to the vector
   (`internal/service/ingest.go` `contentForEmbed`). Large items become
   effectively title-only embeddings. **Plan 2b fix:** hydrate from
   FileStore (`port.FileStore.Get`) before embedding.

2. **`context_embedding` status rows are NOT written in 2a.** The
   schema has the table (migration 0002), but no code populates it —
   the presence of a `vec_<model>` row IS the embedded signal, and
   `context_item.any_embedding` is the coarse signal. **Plan 2b fix:**
   write status rows for retry tracking.

3. **The hybrid e2e test is doubly gated** and is NOT exercised by any
   default `make` target. The RRF fusion path is unit-tested at the
   service layer only. To run the hybrid e2e:
   `CGO_ENABLED=1 go test -tags 'sqlite_fts5,integration,e2e' -run Hybrid ./internal/cli/...`
   with `UNICTX_E2E_HYBRID=1` set and a live Ollama with `bge-m3`
   pulled.

4. **Embedding is synchronous.** Ingest waits up to ~60s for Ollama on
   every Create when the embedder is enabled. **Plan 2b fix:** async
   queue.

5. **Only one embedding model.** The schema supports multi-model
   (`embedding_model` table with `vec_<model>` tables per row), but
   only `bge-m3` is wired. **Plan 2c fix:** runtime model registry.

6. **Only the Ollama provider.** No OpenAI-compat / LMStudio yet.
   **Plan 2d fix.**

7. **No backfill.** Plan 1 items created before enabling
   `embedder.enabled=true` will not be embedded. **Plan 2b fix:**
   `unictx embed backfill` command.

### Deferred to Plan 2b/c/d

Pulled from the plan's "Out of scope" section — still out of scope
after 2a:

- Async embedding queue → **Plan 2b**
- Backfill existing Plan 1 items (`unictx embed backfill`) → **Plan 2b**
- Embedding externalized (FileStore) content → **Plan 2b** (needs
  `FileStore.Get` in `EmbedService`)
- `context_embedding` status rows for retry tracking → **Plan 2b**
- Multi-model registry / runtime DDL → **Plan 2c**
- OpenAI-compat providers (LMStudio, OpenAI, etc.) → **Plan 2d**
- `--mode vector-only` → trivial follow-up, skipped in 2a
