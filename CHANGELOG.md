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

**Plan 2 fix:** vector embeddings make short-query semantics work
naturally (the embedding model handles sub-word meaning without a
minimum-token rule). The LIKE-fallback option was considered and
rejected — LIKE on `title`/`content` would work for ASCII but scans
the whole table, and we'd be ripping it out anyway once vectors land.

Until Plan 2, treat sub-3-char queries as unsupported. Search results
being empty for `部署` is expected, not a bug.

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
