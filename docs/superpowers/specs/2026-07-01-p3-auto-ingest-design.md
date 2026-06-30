# P3 — Auto-Ingest from Claude Code Sessions

> **Status:** Design doc, revision 3 — 12 first-review findings + 4
> second-review findings addressed. Awaiting third-pass review.
>
> **Scope:** Hook-driven auto-ingest of Claude Code transcripts into
> `project` scope as paired raw + AI-summary rows. Manual ingest
> command for backfill. AI summary runs asynchronously via a new
> worker.
>
> **Vision anchor:** [docs/vision.md](../../vision.md) — "project scope
> 一般是自动读取如 Code Agent Session 中的数据". P1 (access direction)
> unblocked this; P3 delivers it.
>
> **Out of scope here:** Multi-tool adapters (Aider/Cursor/Continue),
> streaming per-turn ingest, MCP server, PII redaction, entity/relation
> extraction. All deferred to later phases.

## 1. Goal

When a Claude Code session ends, automatically persist the transcript
into `project` scope with two paired items:

1. **Raw row** — slim, FTS-friendly transcript (user/assistant text +
   tool-call skeletons, large payloads stripped) inline; **full
   verbatim transcript** in FileStore referenced via `content_uri`.
2. **Summary row** — AI-generated semantic summary, written
   asynchronously by a worker.

The pair share a stable `conversation_id` (Claude Code's session UUID);
the summary's `parent_id` points to the raw row. Re-ingest (session
resumed → Stop fires again with grown JSONL) upserts by deterministic
ID and bumps `version` on content change.

## 2. Architecture overview

```
┌─────────────────┐  Stop hook   ┌──────────────────────────┐
│  Claude Code    │ ───────────► │ unictx hook stop         │
│  session ends   │  (stdin JSON)│   ├─ parse payload        │
└─────────────────┘              │   ├─ resolve CWD→project │
                                 │   │   └─ git? auto-create │
                                 │   │   └─ non-git? inbox   │
                                 │   ├─ parse JSONL          │
                                 │   ├─ filter → slim        │
                                 │   ├─ FileStore ← full     │
                                 │   ├─ upsert raw row       │
                                 │   ├─ upsert summary row   │
                                 │   │   (empty content)      │
                                 │   └─ insert context_summary│
                                 │       (status=pending)    │
                                 └──────────┬───────────────┘
                                            │
                                            │ (returns fast)
                                            ▼
                                 ┌──────────────────────────┐
                                 │ SummaryWorkerService      │
                                 │   (separate process or    │
                                 │    `unictx summary worker`)│
                                 │   ├─ poll pending         │
                                 │   ├─ load raw from        │
                                 │   │   FileStore           │
                                 │   ├─ call LLM             │
                                 │   ├─ write summary text   │
                                 │   └─ mark done            │
                                 └──────────────────────────┘
```

The hook process and the worker process are **decoupled**. Hook writes
the pending record; worker drains it. Matches the existing embed
pipeline shape (`IngestService` writes `any_embedding=0` →
`WorkerService` fills it).

## 3. Data model

### 3.1 Existing fields activated

| Field | Current state | P3 use |
|-------|--------------|--------|
| `ContextItem.parent_id` | Always `""` | Summary row points to raw row's id |
| `ContextItem.conversation_id` | Always `""` | Claude Code session UUID (shared by both rows) |
| `ContextItem.confidence` | Hard-coded `1.0` | Summary row gets `< 1.0` (AI-generated) |
| `ContextItem.version` | Starts `1`, never bumped | Bumped on re-ingest when content changes |
| `ContextItem.source_meta` | Always `{}` | `{session_uuid, cwd, turn_count, git_remote, ingest_version}` |
| `ContextItem.any_embedding` | Existing embed pipeline | Unchanged — summary text gets embedded on its own pipeline; raw row's slim content gets embedded too (both flow through existing `EmbedService`) |
| `Source.AGENT` | Enum value, unused | Set on all P3 rows |

### 3.2 Schema additions (migration `0006_auto_ingest.sql`)

**New table: `context_summary`** (mirrors `context_embedding` shape):

```sql
CREATE TABLE IF NOT EXISTS context_summary (
    item_id        TEXT PRIMARY KEY REFERENCES context_item(id) ON DELETE CASCADE,
    status         TEXT NOT NULL,        -- 'pending' | 'done' | 'failed'
    model_slug     TEXT NOT NULL,        -- which summarizer produced this
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT NOT NULL DEFAULT '',
    summarized_at  INTEGER,              -- NULL until status='done'
    updated_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_summary_pending
    ON context_summary(status) WHERE status = 'pending';

UPDATE schema_meta SET value = '6' WHERE key = 'schema_version';
```

Design note: a separate table (not a column on `context_item`) keeps
the retry / status machinery isolated, exactly as `context_embedding`
does for embed status. The pattern is established — we follow it.

**No changes to `context_item`** — every field the design needs
already exists. This is the payoff from the migration's dormant-field
discipline.

**No changes to `project`** for MVP — auto-creation uses the existing
fields (`id`, `name`, `path`, `description`). Per-project ingest
filtering (slim/full/text-only) is deferred; the `Project` table gains
a metadata column when that lands.

### 3.3 ContextItem shapes for a Claude Code session

> **Construction note (🔴):** `items.models.new_context_item`
> (Python) hardcodes three fields that P3 must override **after**
> construction:
>
> - `id=str(uuid.uuid7())` (line 190) — random; P3 needs deterministic
>   `uuid5`.
> - `confidence=1.0` (line 209) — fixed; P3's summary row needs `< 1.0`.
> - `version=1` (line 214) — fixed; P3 bumps on re-ingest.
>
> `ContextItem` is a `slots=True` dataclass; field reassignment is legal
> (`item.id = ...`) but undocumented. `SessionIngestService` does this
> override in one place immediately after the `new_context_item` call —
> not scattered across the codebase. This keeps `new_context_item`'s
> contract intact for non-P3 callers.

**Raw row:**

| Field | Value |
|-------|-------|
| `id` | `uuid5(NAMESPACE_OID, f"claude-code:session:{session_uuid}:raw")` — override after construction |
| `scope` | `Scope.PROJECT` |
| `kind` | `Kind.CONVERSATION_MSG` |
| `source` | `Source.AGENT` |
| `owner_user_id` | current user (from `cfg.user.id`) |
| `project_id` | resolved from CWD (see §5) |
| `conversation_id` | session UUID |
| `content` | slim transcript (≤4 KB inline) |
| `content_uri` | FileStore address (`file://<sha256-hex>`) for **full verbatim** transcript |
| `content_hash` | `sha256:<hex>` — exactly the digest returned by `FileStore.put()` (matches existing convention; **not** bare hex) |
| `source_meta` | `{"session_uuid", "cwd", "turn_count", "git_remote", "ingest_version"}` |
| `confidence` | `1.0` (raw is ground truth — no override needed) |
| `version` | starts at `1` (constructor default); bumped when full transcript SHA-256 changes |
| `any_embedding` | existing pipeline; not P3's concern |

**Summary row:**

| Field | Value |
|-------|-------|
| `id` | `uuid5(NAMESPACE_OID, f"claude-code:session:{session_uuid}:summary")` — override after construction |
| `scope` | `Scope.PROJECT` (inherits from raw) |
| `kind` | `Kind.MEMORY` |
| `source` | `Source.AGENT` |
| `project_id` | same as raw |
| `conversation_id` | session UUID |
| `parent_id` | raw row's id |
| `content` | summary text, empty until worker fills it (≤4 KB inline; if longer, externalized normally) |
| `word_count` | `0` at construction (empty content); **updated by worker** when summary is filled (§7.1) |
| `confidence` | `0.7` — override after construction (AI-generated; **exact value TBD** in implementation — see §11.3) |
| `version` | starts at `1`; bumped when summary regenerated |
| `source_meta` | `{"summarizer_model", "summarized_at", "raw_ingest_version"}` |

**`ingest_version` initial value (🟢):** monotonically increasing integer
scoped to a session. Initial ingest sets `ingest_version=1`. Each re-ingest
(§9.2 Path 5) bumps it by 1. Stored in `source_meta.ingest_version` so the
value is visible without a separate column. Purpose: lets the worker / UI
distinguish "first ingest" from "Nth re-ingest" and lets the user audit how
many times a session was processed.

`uuid5` IDs make re-ingest idempotent without a separate lookup table:
same session UUID always produces the same `ContextItem.id` pair.

### 3.4 Schema impact: `content` + `content_uri` coexistence

Current contract: items have either inline `content` (≤4 KB) **or**
externalized `content_uri` (>4 KB), not both. P3's raw row breaks this:
slim content stays inline **and** `content_uri` references the full
verbatim transcript in FileStore.

**Resolution:** document `content_uri` as a **supplementary** reference
for `kind=CONVERSATION_MSG` rows with `source=AGENT`, not a replacement.
Concretely:

- `ItemService.Get` hydration: for these rows, always return inline
  `content`; expose `content_uri` as an opt-in "fetch full transcript"
  path via a new helper (`ItemService.get_full_transcript(item_id)` or
  similar).
- `ReindexFTSService`: continues to index inline `content` for FTS —
  no change. The slim content is the FTS source of truth.
- **`SessionIngestService` bypasses `IngestService.create()` entirely**
  and calls `ContextRepo.create()` directly (🟡). The existing
  `IngestService.create()` externalize branch (`python/src/unictx/items/
  ingest.py` lines 234-241) is **mutually exclusive**: it sets
  `content_uri` *xor* `content`, never both. P3's raw row needs both
  fields populated, which that branch cannot produce. Rather than wedge
  a special case into `IngestService.create()`, P3 introduces a new
  `SessionIngestService` that owns the full-write path: FileStore puts,
  `ContextItem` construction + `.id`/`.confidence` overrides, and direct
  `ContextRepo.create()`. The PDF rollback machinery and content-size
  logic in `IngestService.create()` are not relevant to P3.

This is a localized semantic shift, not a refactor of the inline-or-
externalized invariant for other kinds.

## 4. Hook subsystem

### 4.1 Hook entry point: `unictx hook stop`

New CLI command. Reads Claude Code's Stop hook payload from stdin:

```json
{
  "session_id": "abc-123-...",
  "transcript_path": "/Users/.../session.jsonl",
  "cwd": "/Users/.../project-dir",
  "stop_hook_active": false
}
```

Behavior:

1. Parse stdin JSON. On parse failure: log + exit 0 (never block
   Claude Code UI).
2. Resolve CWD → project (§5). Non-git → stage transcript to inbox
   (§5.4), exit 0.
3. Read transcript JSONL.
4. Filter to slim version (§6).
5. Write full transcript to FileStore → get `file://<hex>` URI and
   `sha256:<hex>` digest. If slim content overflows the 4 KB inline
   limit (§6.3), also `fs.put(slim_md_bytes, "text/markdown")` and
   capture `(slim_uri, slim_digest)` for `source_meta.slim_uri`.
6. **Atomic three-step DB write (🔴):** `SessionIngestService.write()`
   wraps the following in a single `BEGIN … COMMIT` transaction
   (sqlite3 connection in manual-commit mode):

   ```
   BEGIN;
   -- (a) raw row upsert
   INSERT INTO context_item (id, scope, kind, source, …, content, content_uri, …)
     VALUES (?, ?, ?, ?, …, ?, ?, …)
     ON CONFLICT(id) DO UPDATE SET … ;
   -- (b) empty summary row upsert
   INSERT INTO context_item (id, scope, kind, source, parent_id, content, …)
     VALUES (?, ?, ?, ?, ?, '', …)
     ON CONFLICT(id) DO UPDATE SET content='', any_embedding=0 ;
   -- (c) summary status row
   INSERT INTO context_summary (item_id, status, attempts, updated_at)
     VALUES (?, 'pending', 0, ?)
     ON CONFLICT(item_id) DO UPDATE SET status='pending', attempts=0 ;
   COMMIT;
   ```

   Rationale: if any of the three writes fails (e.g. disk full mid-write,
   `ON CONFLICT` schema drift), the user is left in a torn state — raw
   row present but no summary row, or summary row orphaned from
   `context_summary` status. The transaction makes the three-step
   atomic. On `OperationalError` from sqlite3 the whole write is rolled
   back, the hook surfaces the error to stderr, and exits 0 (per the
   rule below). The next Stop hook firing re-runs the same upserts
   idempotently.

   FileStore writes (step 5) happen **before** the transaction because
   they cannot be rolled back via SQLite. On DB transaction failure the
   orphaned FileStore blobs are acceptable: they are refcount-zero and
   will be cleaned by a future janitor; they do not corrupt the DB.

7. Exit 0 (always — hook failures must not block the user's session
   end). Errors go to stderr.

**Timeout:** Stop hook has a wall-clock budget from Claude Code
(typically 30s). All P3 hook work fits comfortably: JSONL parse +
slim filter + FileStore write + one transaction of 3 SQL upserts. LLM
call is **not** in this path — it's async via worker.

### 4.2 Hook installation: `unictx hook install stop`

New CLI command. Writes the Stop hook entry into Claude Code's
settings:

- `~/.claude/settings.json` (user-global, default), or
- `.claude/settings.json` in CWD (project-local, with `--scope project`)

**Reference (🟢):** Claude Code's hook system is documented in the
official docs at
`https://docs.claude.com/en/docs/claude-code/hooks`. The Stop hook
fires after the assistant finishes responding and the session is about
to end. Payload schema, event types, and settings.json structure all
follow that reference. If Anthropic changes the schema, P3's parser
must update accordingly — the spec hardcodes against the current
published shape.

Entry shape (per current Claude Code docs):

```json
{
  "hooks": {
    "Stop": [
      {
        "command": "unictx hook stop",
        "timeout_seconds": 30
      }
    ]
  }
}
```

The installer merges into existing settings (does not overwrite). If a
`unictx hook stop` entry already exists, no-op (idempotent).

`unictx hook uninstall stop` reverses it.

### 4.3 Failure modes

| Failure | Behavior |
|---------|----------|
| stdin JSON parse error | stderr line, exit 0 |
| Transcript file missing / unreadable | stderr line, exit 0 |
| Non-git CWD | stage to inbox, exit 0 |
| DB locked (sqlite3.OperationalError) | retry once after 100ms; give up + stderr, exit 0 |
| FileStore write failure | abort ingest, stderr, exit 0 |
| Any unexpected exception | stderr with traceback, exit 0 |

The "exit 0 always" rule is load-bearing: Claude Code surfaces non-zero
exits as UI interruptions. Hook visibility goes through stderr (visible
if the user looks) + a future `unictx ingest log` query, never through
exit codes.

## 5. Project binding

### 5.1 Resolution algorithm

```python
def resolve_project(cwd: Path, repo: ProjectRepo) -> Project | None:
    # 1. Exact path match (handles user-registered projects)
    existing = repo.find_by_path(str(cwd))
    if existing:
        return existing

    # 2. Is cwd inside a git repo?
    git_root = _find_git_root(cwd)  # walks up looking for .git/
    if git_root is None:
        return None  # caller routes to inbox

    # 3. Exact path match on git root (handles cwd-inside-repo)
    existing = repo.find_by_path(str(git_root))
    if existing:
        return existing

    # 4. Auto-create from git repo metadata
    name = _derive_project_name(git_root)  # basename; remote URL if available
    return repo.create(new_project(name=name, path=str(git_root), description=""))
```

CWD inside a subdirectory of a git repo binds to the **repo root**, not
the subdirectory. This matches the user's mental model ("this codebase"
is the project, not "this folder I happened to be in").

### 5.2 Project name derivation

Default: `git_root.name` (basename). If `git remote get-url origin`
returns a parseable URL, prefer the repo name from it (e.g.
`git@github.com:zead/uni-context` → `uni-context`). On any error
(non-git, no remote, parse failure), fall back to basename.

### 5.3 Manual registration: `unictx project register`

New command for users who want to pre-create or override:

```
unictx project register <path> [--name NAME] [--description DESC]
```

- `<path>` must exist; becomes `Project.path`.
- `--name` defaults to basename of path.
- `--description` defaults to empty; user can edit later (future
  `unictx project update` command — not in P3).

If a Project with the same path already exists, no-op with a notice
(unless `--force`, which overwrites name/description).

Use cases: pre-create before first session; rename auto-created
projects; attach description.

### 5.4 Non-git CWD: inbox file staging

When `resolve_project` returns `None` (CWD is not in any git repo and
no Project is registered for it):

1. Copy transcript JSONL to
   `<data_dir>/inbox/<timestamp>-<session_uuid>.jsonl`.
2. stderr: `unictx: cwd X is not in a git repo; transcript staged to
   <path>. Run 'unictx ingest session <path> --project <id>' to
   ingest.`
3. Exit 0.

The inbox never enters the DB. It's a safety net — no data lost, user
can triage later. Periodic cleanup of old inbox files is the user's
responsibility (no auto-expiration in P3).

Manual ingest of an inbox file:

```
unictx ingest session <path> --project <project-id>
```

This is the same command used for backfill — it just takes an explicit
`--project` because the path-based resolution won't fire for an inbox
file.

## 6. Content filtering

### 6.1 Filtering rules

For each event in the JSONL transcript:

| Event type | Action |
|-----------|--------|
| `user` with text blocks | Keep text verbatim |
| `user` with `tool_result` blocks | Drop the tool_result content; keep a one-line marker `[tool_result: <tool_use_id>, stripped N chars]` |
| `assistant` text blocks | Keep verbatim |
| `assistant` `tool_use` blocks | Keep `tool_name` + short args (file paths, command strings ≤200 chars); strip args >200 chars to `[arg stripped, N chars]` |
| `system` events | Keep `summary` / `pin` events verbatim; drop other system metadata |
| Anything else | Drop |

### 6.2 Slim output format

Markdown-ish, streaming-friendly:

```
## user
<text>

## assistant
<text>

## assistant → tool: Read
file_path: src/foo.py

## user [tool_result: Read, stripped 2341 chars]

## assistant
<text>
```

Target size: typically <4 KB for a normal session. If the slim version
itself exceeds 4 KB (long sessions), it overflows naturally — the inline
`content` field stores the first ~3.5 KB and a trailing `... [slim
truncated, see content_uri for full]` marker; the **full slim version**
also lives in FileStore alongside the verbatim transcript (two separate
blobs, two separate SHA-256 hashes — see §6.3).

### 6.3 FileStore layout

`FileStore.put(bytes, mime)` content-addresses by SHA-256 of the bytes
and returns `("file://<hex>", "sha256:<hex>")`. The `_hash_from_uri`
helper (`python/src/unictx/storage/filestore.py` lines 94-101) **strictly
requires** exactly 64 hex characters after `file://` — suffix conventions
like `<hex>.slim.md` would be rejected at hydration time.

Per session, P3 calls `FileStore.put()` **up to twice** (🔴):

- **Always:** `fs.put(full_jsonl_bytes, "application/x-ndjson")` →
  `(full_uri, full_digest)`. Stored on raw row's `content_uri` /
  `content_hash`.
- **Only when slim content overflows the 4 KB inline limit:**
  `fs.put(slim_md_bytes, "text/markdown")` → `(slim_uri, slim_digest)`.
  The slim URI is stored in raw row's `source_meta.slim_uri` (a field
  the hook populates only on overflow).

Rationale for storing slim URI in `source_meta` rather than as a separate
column: this avoids a schema migration, and slim-overflow is the rare
case — most sessions produce a slim version that fits inline.

Re-ingest (§9.2 Path 5) must `fs.delete(old_full_uri)` before the new
`put` so refcounts decrement correctly (🟡, see §9.2). The slim blob
follows the same rule when it exists.

**No `.slim.md` suffix convention.** FileStore keys are bare hashes;
mime type lives in the sidecar `.meta` JSON. Inspection tools that need
to distinguish transcript types should query the DB (`source=AGENT` +
`kind=CONVERSATION_MSG`), not the FileStore layout.

## 7. AI summary pipeline

### 7.1 Worker: `SummaryWorkerService`

New service, mirrors `WorkerService` for embeddings. Lifecycle:

1. Polls `context_summary WHERE status = 'pending'` (configurable
   interval, default 5s).
   - **Note (🟢):** This is a **different polling semantics** from the
     embed worker. `embed/worker.py` line 89 polls `list_failed` —
     i.e. re-tries rows that the embed pipeline already attempted and
     failed. The summary worker polls `'pending'` because summary
     generation is the **initial** processing step for the summary row,
     not a retry of a previous failure. Once `status='done'`, the row
     is never re-polled. Once `status='failed'` (3-strike), it requires
     manual intervention (future command). Do not unify the two polling
     predicates.
2. For each pending row:
   - Load the parent raw row.
   - Load the full transcript from FileStore via `content_uri`.
   - Build the prompt (§7.3).
   - Call LLM (`SummarizerClient`, §8).
   - On success: in a single DB transaction, UPDATE the summary row's
     `content = summary_text`, `word_count = count_words(text)` (🟡),
     `any_embedding = 0` (so the embed pipeline can pick it up — see
     §11.2 for why this is the worker's responsibility). Then UPDATE
     `context_summary.status='done'`, `summarized_at=now`,
     `attempts += 1`. After the transaction commits, call
     `embed_svc.embed_item(item.id, item.title, summary_text)` (outside
     the transaction — same pattern as `IngestService.create`).
   - On failure: `attempts += 1`, `last_error=str(exc)`. After 3
     attempts, `status='failed'` (manual retry via future command).
3. Cooperative cancellation via `threading.Event` (same pattern as
   existing workers).

### 7.2 Worker invocation

Two modes:

- **Foreground:** `unictx summary worker [--once]` — drains pending
  and exits (or runs forever). Mirrors `unictx embed worker`.
- **Background daemon:** out of scope for P3. User runs the foreground
  worker manually or via a launchd/cron wrapper. P3.2 may add a real
  daemon mode.

### 7.3 LLM prompt

Single-shot, text-in/text-out:

```
Summarize the following Claude Code session. Focus on:
- What the user was trying to accomplish
- Key decisions made
- Files changed and why
- Any blockers or unresolved issues

Keep the summary under 500 words. Use bullet points.

--- TRANSCRIPT ---
<full transcript from FileStore>
```

The prompt template is configurable via `summarizer.prompt_template`
(§8.1); the above is the default. Variables: `{transcript}`.

Output: plain text written verbatim to summary row's `content` (no
JSON parsing, no structured extraction — that's a later phase).

### 7.4 Failure / degradation

| Failure | Behavior |
|---------|----------|
| `summarizer.enabled = false` or section absent in config | Hook writes raw row only; no summary row, no `context_summary` entry |
| LLM call fails (network, 4xx, 5xx) | `attempts += 1`, retry on next poll. After 3 attempts: `status='failed'`, summary row's `content` stays empty |
| LLM returns empty / over-length response | Treat as failure, retry |
| Raw row missing or FileStore missing | Mark `status='failed'` with `last_error`; do not retry (data issue, not transient) |

Raw row is never affected by summary pipeline failures — it's the
ground truth. Users always have FTS over the slim version + full recall
via FileStore, even if every summary attempt fails.

## 8. Configuration

### 8.1 New `summarizer:` section in `config.yaml`

```yaml
summarizer:
  enabled: true
  provider: openai-compat       # openai | openai-compat | ollama
  base_url: http://localhost:1234/v1
  model: gpt-4o-mini
  api_key: sk-xxx
  prompt_template: ""            # optional; default shown in §7.3
```

**Defaults logic** mirrors `EmbedderConfig`:

- `enabled: false` (or section absent) → no summary pipeline; raw-only
  ingest. Hook still writes raw row.
- `enabled: true` with no other fields → defaults:
  - `provider: ollama`
  - `base_url: http://localhost:11434`
  - `model: llama3.1` (or similar; **exact default TBD** — needs a
    model that fits a "summarize 50KB transcript" task)
  - `api_key: ""`
- `enabled: true` + `provider: openai` (or `openai-compat`) →
  `base_url` defaults to `http://localhost:1234/v1` (same alias
  treatment as EmbedderConfig post-review fix).

### 8.2 No changes to existing config sections

`embedder:` is unchanged. `pdf:` is unchanged. The two pipelines
(embed, summarize) are independent — disabling one does not affect the
other.

### 8.3 Pydantic model

New `SummarizerConfig` BaseModel in `config.py`. **Same pattern as**
`EmbedderConfig` (not "same fields" — the two differ in detail):

- **Shared:** `enabled: bool = False`, `provider: str = ""`,
  `base_url: str = ""`, `model: str = ""`, `api_key: str = ""`;
  `model_config = _STRICT` (`extra="forbid"`); same
  `@model_validator(mode="after")` `apply_defaults` shape with the
  `enabled=False` short-circuit + provider-keyed `base_url` map
  (`ollama`/`openai`/`openai-compat`).
- **Summarizer drops:** `dimension` (embedding-only — vector size
  doesn't apply to a text-out pipeline).
- **Summarizer adds:** `prompt_template: str = ""` (summarizer-only;
  empty default falls back to the §7.3 built-in template at call
  time).

`Config.summarizer: SummarizerConfig = Field(default_factory=SummarizerConfig)`.

## 9. Dedup and idempotency

### 9.1 Deterministic IDs

```python
RAW_ID = uuid.uuid5(uuid.NAMESPACE_OID, f"claude-code:session:{session_uuid}:raw")
SUMMARY_ID = uuid.uuid5(uuid.NAMESPACE_OID, f"claude-code:session:{session_uuid}:summary")
```

`uuid5` is deterministic: same session UUID always produces the same
ID pair. No lookup table needed.

### 9.2 Upsert semantics

On every ingest (Stop hook or manual):

1. Compute SHA-256 of full transcript.
2. Look up raw row by deterministic ID.
3. If absent: INSERT (initial ingest).
4. If present and `content_hash` matches: no-op for raw row. Summary
   row + `context_summary` left untouched (summary already correct).
5. If present and `content_hash` differs: UPDATE raw row, bump
   `version`. Reset summary: clear summary row's `content`, set
   `context_summary.status='pending'`, `attempts=0` so the worker
   regenerates with the new transcript. **Refcount cleanup (🟡):**
   re-ingest leaks a refcount on the old verbatim blob if we forget to
   decrement. Safe ordering (because FileStore writes cannot be rolled
   back via SQLite):

   ```
   # (1) outside DB transaction: put NEW first (new blob exists)
   new_uri, new_digest = fs.put(new_full_bytes)
   # (2) inside BEGIN … COMMIT: UPDATE raw row's content_uri/content_hash
   #     — capture old_uri via SELECT before UPDATE
   # (3) AFTER COMMIT succeeds: fs.delete(old_uri) — old blob refcount-1
   ```

   Why this order: if (2) aborts, the DB still references `old_uri`
   (unchanged) and `new_uri` is orphaned-but-refcount-zero — a janitor
   can clean it. If we instead deleted `old_uri` first then DB-aborted,
   the DB would reference a deleted blob — corruption. Apply the same
   sequence to `source_meta.slim_uri` when it was previously set.
   **Symmetric cleanup (🟡):** if the **new** slim content fits inline
   (≤4 KB) but the **old** row had `source_meta.slim_uri` set, the
   UPDATE step must also **drop the `slim_uri` key from
   `source_meta`** in the same UPDATE — otherwise the DB keeps a
   dangling URI pointing to a blob that step (3) just deleted.

### 9.3 Session resume

Claude Code's session resume continues the same JSONL — the file grows.
Stop hook fires again at end of the resumed session, sees the longer
file, computes a different SHA-256, triggers §9.2 path 5 (re-summarize).

## 10. Write-side access boundary

P1 enforces **read-side** convergence. P3 introduces an **AGENT-source
write path** for the first time. Decision (declared in brainstorming):

**Rule:** `source=AGENT` + `scope=project` + `project_id` matching the
auto-created Project for the session's CWD is **always allowed**. No
grant check.

**Rationale:**
- The user explicitly ran Claude Code in that directory; running it
  there is the consent.
- Grant table semantics are read-side (which scopes an actor can see).
  Writes-by-source is a different axis; conflating them would
  complicate P1's model.
- Disabling auto-ingest for a specific project is a **per-project
  config** concern (e.g. `Project.metadata.ingest_enabled = false`),
  not a grant concern. Deferred — P3 always ingests when hook fires.

This is recorded as a **write-side rule** in `new_context_item`'s
documentation, but **no enforcement code** in P3 — the rule is
structural (AGENT writes only ever happen via the P3 hook path, which
always sets `scope=project` + matched `project_id`). If a future
WEBHOOK/SYNC source wants to write to other scopes, that source's
boundary decision lands then.

## 11. Open questions (resolve in implementation plan)

### 11.1 Summary worker: extend `WorkerService` or new service?

Two options:

- **Extend** `WorkerService` with a `job_kind` discriminator (embed vs
  summary). One process, one poll loop, dispatches to embed or summary
  per row.
- **New** `SummaryWorkerService` (separate process, separate poll).

Trade-off: extending shares infrastructure but couples embed-failures
to summary-failures (one bug in summary path can stall embeds);
separating duplicates the poll loop but isolates failures.

**Default:** new `SummaryWorkerService`. The pattern is established
(EmbedService + WorkerService pair); mirroring it is consistent and
keeps the summary path free to evolve (e.g. adding entity extraction
later) without touching embed.

**Caveat (🟢):** the new service polls `'pending'` (initial
processing) while `WorkerService` polls `'failed'` (retry after
synchronous-at-ingest failure). These are **different semantics** —
unifying them via a `job_kind` discriminator (the "extend" option)
would require both predicates in the same query, which complicates the
poll loop. The "new service" option sidesteps this entirely. See §7.1
for the polling-predicate note.

### 11.2 Embed pipeline interaction with summary row (🟡)

The existing embed pipeline is **synchronous-at-ingest** plus
**retry-on-failure**:

- `IngestService.create()` calls `embed_item()` synchronously at row
  creation time (`python/src/unictx/items/ingest.py` line 303). If it
  succeeds: `any_embedding=1`. If it fails: `EmbedService.embed_item`
  records `status='failed'` and raises; the warning is logged and
  ingest continues.
- `WorkerService` (`embed/worker.py` line 89) polls `list_failed` to
  retry. There is **no `list_pending`** in the embed pipeline — rows
  are not lazily discovered.

This is incompatible with the summary row's lifecycle ("empty at
construction, filled by worker later"). Two questions:

**Q1: What happens if `embed_item` is called on empty content?**
`EmbedService.embed_item` (`embed/service.py` lines 120-124) composes
`title + "\n\n" + content`, strips, and **raises
`RuntimeError(f"embed: empty text for item {item_id}")` with
`status='failed'`** if the result is empty. So a naive synchronous
embed call at summary-row construction would permanently mark the row
`status='failed'` and the worker would retry forever (per the worker
docstring: "no max-attempts cap").

**Q2: Then how does the summary row get embedded after the worker
fills content?**

The `SummaryWorkerService` is responsible. After it writes summary
text to the row, it explicitly invokes the embed pipeline:

```python
# Inside SummaryWorkerService, after the LLM call succeeds:
with db.transaction():
    item.content = summary_text
    item.word_count = count_words(summary_text)
    item.any_embedding = 0          # reset: will need re-embed
    repo.update(item)
    ctx_summary.status = 'done'
    ctx_summary.attempts += 1
    ctx_summary.summarized_at = now
    summary_repo.update_status(ctx_summary)

# Outside the transaction: synchronous embed call (mirrors IngestService)
embed_svc.embed_item(item.id, item.title, summary_text)
```

If the embed call fails (network, empty text, etc.) it records
`status='failed'` in `context_embedding` and the existing
`WorkerService` retry loop drains it — the summary row's status
remains `'done'`; the embed retry is decoupled.

**What about the raw row at hook time?** Raw row has non-empty slim
content, so `SessionIngestService` calls `embed_item()` synchronously
at ingest (mirroring `IngestService.create()`). If it fails, the
existing `WorkerService` retries — same as any other ingest.

**What about the summary row at hook time?** `SessionIngestService`
does **not** call `embed_item` on the empty summary row. The summary
row starts with `any_embedding=0` (constructor default) and stays
that way until the summary worker fills content + invokes embed.

This is a **new pattern**: `SessionIngestService` owns the embed-call
decision per row (yes for raw, no for empty summary), rather than the
default "always call embed on every new row" of `IngestService.create`.

### 11.3 Summary `confidence` exact value

Brainstorming locked `< 1.0`. Exact value TBD. Candidates:
- `0.7` — "trustworthy but verify"
- `0.5` — neutral
- Configurable per `summarizer.confidence`

**Default:** `0.7` (hardcoded for MVP). Make configurable if user
pushes back. Retrieval ranking uses confidence as a weak signal — exact
value is not load-bearing.

### 11.4 Default summarizer model

`gpt-4o-mini` is reasonable for OpenAI-compatible; `llama3.1:8b` for
Ollama. Both handle 50 KB transcripts. **Decision deferred to plan** —
the default only matters if the user enables summarizer without
setting a model, which is rare.

### 11.5 Slim truncation marker wording

`"... [slim truncated, see content_uri for full]"` — fine for MVP,
refine later if it leaks into UI surfaces badly.

### 11.6 Inbox cleanup

P3 ships no auto-expiration. `unictx inbox list` / `unictx inbox clean
--older-than 30d` are obvious follow-ups, deferred.

## 12. CLI surface summary

New commands added in P3:

| Command | Purpose |
|---------|---------|
| `unictx hook stop` | Stop hook entry; reads stdin JSON, ingests transcript |
| `unictx hook install stop [--scope user\|project]` | Installs the Stop hook into Claude Code settings |
| `unictx hook uninstall stop [--scope user\|project]` | Removes the Stop hook |
| `unictx ingest session <path> [--project ID]` | Manual ingest of a JSONL transcript; backfill / inbox triage |
| `unictx project register <path> [--name NAME] [--description DESC]` | Pre-create or override a Project |
| `unictx project list` | List Projects (auto-created + manually registered) |
| `unictx summary worker [--once]` | Foreground summary-pipeline drain |

All support `--json` per global flag convention.

## 13. Testing strategy

### 13.1 Unit tests

- `tests/items/test_models.py` — `uuid5` ID determinism (same session
  UUID → same raw/summary IDs).
- `tests/items/test_project_binding.py` — `resolve_project` algorithm:
  exact match, git-root walk-up, auto-create, non-git returns None.
- `tests/ingest/test_transcript_filter.py` — slim filter rules from §6
  as a table-driven pure-function test.
- `tests/ingest/test_session_ingest.py` — `SessionIngestService`:
  initial ingest, re-ingest no-op (same hash), re-ingest upsert
  (different hash), idempotency under repeated calls.
- `tests/summary/test_summary_worker.py` — pending → done transition,
  LLM failure retry, 3-strike failed status, raw row never mutated.
- `tests/config/test_summarizer_config.py` — defaults logic mirrors
  EmbedderConfig tests; openai/openai-compat alias.

### 13.2 Service tests

- `tests/cli/test_hook_stop.py` — hook entry: stdin JSON parse, project
  resolution branches, FileStore writes, exit code 0 on every failure
  mode (load-bearing — non-zero would break Claude Code).
- `tests/cli/test_ingest_session.py` — manual command backfill path.
- `tests/cli/test_project_register.py` — registration + idempotency.
- `tests/cli/test_hook_install.py` — settings.json merge semantics,
  idempotency, uninstall.

### 13.3 Integration

- End-to-end Stop hook simulation: feed a fixture JSONL through
  `unictx hook stop`, assert raw + summary + pending context_summary
  all written, project auto-created.
- Re-ingest simulation: run twice with same transcript (no-op) and
  with grown transcript (upsert + re-summary).
- FTS over ingested raw row: slim content is searchable.
- FTS over summary row after worker drain: summary is searchable.

### 13.4 Anti-leak regression

`tests/cli/test_hook_stop.py::test_non_git_cwd_stages_to_inbox_not_db`
— non-git CWD writes to inbox, never to DB. Load-bearing: failure
means we're ingesting random-dir sessions, violating the git-only
contract from the user's brainstorming decision.

## 14. Out of scope (deferred)

- **Other Agent tools** (Aider, Cursor, Continue) — adapter layer
  added when needed; P3 ships Claude Code only.
- **Streaming per-turn ingest** — Stop-hook-end-of-session is the only
  trigger.
- **PII / secret redaction** — local data, user's responsibility.
  Document the risk. Future regex/LLM redaction layer.
- **Per-project ingest filtering** (`slim` / `full` / `text-only`) —
  uniform slim filter for P3.
- **Inbox auto-expiration** — manual cleanup only.
- **Project metadata** (`ingest_enabled`, `filter`, etc.) — schema
  addition when needed.
- **Summary re-generation triggers beyond content change** (model
  upgrade, prompt change) — P3 only regenerates on content change.
- **Daemon-mode summary worker** — foreground only; launchd/cron
  wrapper is the user's call.
- **Multi-worktree consolidation** — each worktree is a separate
  Project (different path). Document.
- **Entity / relation extraction** — vision-aligned but separate
  phase; requires `RelationRepo` design first.
- **MCP server / webhook ingest** — orthogonal phase.

## 15. Migration strategy

- **Migration 0006** adds `context_summary` table. Pure addition; no
  existing data affected. `migrated_db` fixture picks it up
  automatically via incremental migrate.
- **No data backfill.** Existing items don't get `parent_id` /
  `conversation_id` populated — those fields stay empty for non-P3
  items. Search and retrieval code must handle empty values (it
  already does).
- **Hook installation is opt-in.** Users must run
  `unictx hook install stop` after upgrading. P3 does not silently
  modify Claude Code settings on first run.

## 16. Rollout

P3 is feature-flagged by configuration, not by code:

- No `summarizer:` section → raw-only ingest, no summary pipeline.
  P3 is effectively inert for summary purposes.
- No Stop hook installed → no auto-ingest happens. P3 only activates
  when the user explicitly opts in via `unictx hook install stop`.

The combination gives four deployment shapes:

| Hook installed? | Summarizer configured? | Behavior |
|-----------------|------------------------|----------|
| No | No | P3 inert |
| No | Yes | Manual ingest only (`unictx ingest session`); summary pipeline runs |
| Yes | No | Auto-ingest raw only |
| Yes | Yes | Full P3 |

This makes P3 safe to ship "always" — users opt in via config + hook
installation, not via feature flags.

---

## Self-review notes

- **Placeholder scan:** §11 lists open questions, all with default
  answers; no "TODO" / "TBD" outside that section except where the
  value is genuinely deferred (default model name, exact confidence).
- **Internal consistency:** §3.4's content/content_uri coexistence is
  the only schema shift; §6.3 + §7 + §9 all reference it consistently.
  §11.2 has been rewritten to match the actual embed pipeline shape
  (synchronous-at-ingest + retry-on-failure, no lazy scan).
- **Codebase consistency (P3 review findings addressed):**
  - 🔴 #1, #2: `new_context_item` hardcodes `id` (uuid7) and
    `confidence` (1.0); §3.3 construction note documents the post-
    construction override on the slots dataclass.
  - 🔴 #3: §6.3 replaces the `.slim.md` suffix convention with two
    separate `FileStore.put()` calls (FileStore's `_hash_from_uri`
    rejects non-64-hex URIs).
  - 🔴 #4: §4.1 wraps the three-step write (raw + summary + context_summary)
    in an explicit `BEGIN … COMMIT` transaction; FileStore writes
    stay outside the transaction (cannot be rolled back via SQLite).
  - 🟡 #5: §11.2 spells out exactly when `embed_item` is called on the
    summary row (worker's responsibility, after content fill).
  - 🟡 #6: §9.2 documents the safe ordering — put-new, DB UPDATE,
    delete-old — so DB-abort never leaves a dangling reference.
  - 🟡 #7: §3.4 + §4.1 make explicit that `SessionIngestService`
    bypasses `IngestService.create()` (whose externalize branch is
    mutually exclusive) and writes via `ContextRepo.create()` directly.
  - 🟡 #8: §7.1 includes `word_count` UPDATE in the worker's success
    path (constructor default is 0).
  - 🟡 #9: §3.3 raw row clarifies `content_hash` format as
    `sha256:<hex>` matching `FileStore.put()` return.
  - 🟢 #10: §7.1 + §11.1 note that summary worker polls `'pending'`
    while embed worker polls `'failed'` — different semantics.
  - 🟢 #11: §4.2 cites Claude Code's official hooks documentation.
  - 🟢 #12: §3.3 defines `ingest_version` initial value (1) and bump
    rule (per re-ingest).
- **Codebase consistency (revision 3 — second review):**
  - 🟡 #13: §4.1 summary row `ON CONFLICT` clause now resets
    `any_embedding=0` alongside `content=''`, so re-ingest before
    worker re-fill can't expose stale vectors via SearchService.
  - 🟡 #14: §8.3 `SummarizerConfig` description corrected — drops
    EmbedderConfig's `dimension`, adds summarizer-only `prompt_template`.
  - 🟢 #15: §11.2 RuntimeError wording matches actual code
    (`f"embed: empty text for item {item_id}"`, includes id).
  - 🟢 #16: §9.2 documents symmetric slim_uri cleanup — drop the key
    from `source_meta` if new slim fits inline but old had overflow.
- **Scope check:** single subsystem (auto-ingest), single source
  (Claude Code), single new table. Fits one plan.
- **Ambiguity check:** "non-git CWD" is fully specified (§5.4); "hook
  failure" is fully specified (§4.3); "summary failure" is fully
  specified (§7.4). The two genuinely open items (worker extend-vs-
  new, default model) are flagged with defaults.
