# P3 — Auto-Ingest from Claude Code Sessions

> **Status:** Design doc, awaiting user review.
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

**Raw row:**

| Field | Value |
|-------|-------|
| `id` | `uuid5(NAMESPACE_OID, f"claude-code:session:{session_uuid}:raw")` |
| `scope` | `Scope.PROJECT` |
| `kind` | `Kind.CONVERSATION_MSG` |
| `source` | `Source.AGENT` |
| `owner_user_id` | current user (from `cfg.user.id`) |
| `project_id` | resolved from CWD (see §5) |
| `conversation_id` | session UUID |
| `content` | slim transcript (≤4 KB inline) |
| `content_uri` | FileStore address for **full verbatim** transcript |
| `content_hash` | SHA-256 of full transcript |
| `source_meta` | `{"session_uuid", "cwd", "turn_count", "git_remote", "ingest_version"}` |
| `confidence` | `1.0` (raw is ground truth) |
| `version` | bumped when full transcript SHA-256 changes |
| `any_embedding` | existing pipeline; not P3's concern |

**Summary row:**

| Field | Value |
|-------|-------|
| `id` | `uuid5(NAMESPACE_OID, f"claude-code:session:{session_uuid}:summary")` |
| `scope` | `Scope.PROJECT` (inherits from raw) |
| `kind` | `Kind.MEMORY` |
| `source` | `Source.AGENT` |
| `project_id` | same as raw |
| `conversation_id` | session UUID |
| `parent_id` | raw row's id |
| `content` | summary text, empty until worker fills it (≤4 KB inline; if longer, externalized normally) |
| `confidence` | `0.7` (AI-generated; **exact value TBD** in implementation — see §11.3) |
| `version` | bumped when summary regenerated |
| `source_meta` | `{"summarizer_model", "summarized_at", "raw_ingest_version"}` |

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
- `IngestService` for P3 rows: writes both fields explicitly (does
  **not** run through the existing ">4 KB → externalize" branch).

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
5. Write full transcript to FileStore → get hash.
6. Upsert raw row + empty summary row + `context_summary(status='pending')`.
7. Exit 0 (always — hook failures must not block the user's session
   end). Errors go to stderr.

**Timeout:** Stop hook has a wall-clock budget from Claude Code
(typically 30s). All P3 hook work fits comfortably: JSONL parse +
slim filter + FileStore write + 3 SQL upserts. LLM call is **not** in
this path — it's async via worker.

### 4.2 Hook installation: `unictx hook install stop`

New CLI command. Writes the Stop hook entry into Claude Code's
settings:

- `~/.claude/settings.json` (user-global, default), or
- `.claude/settings.json` in CWD (project-local, with `--scope project`)

Entry shape:

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
also lives in FileStore alongside the verbatim transcript (two files,
two hashes — or one combined file; see §6.3).

### 6.3 FileStore layout

Two files per session, both content-addressed by SHA-256:

- `<sha256>` — **full verbatim transcript** (raw JSONL concatenated or
  slightly cleaned). Referenced by raw row's `content_uri`.
- `<sha256>.slim.md` — **slim version**. Referenced only when slim
  overflows the 4 KB inline limit; otherwise the slim version lives
  inline in `content` and is not separately stored.

The `.slim.md` suffix convention keeps FileStore self-describing —
inspection tools can distinguish transcript types without reading the
DB.

## 7. AI summary pipeline

### 7.1 Worker: `SummaryWorkerService`

New service, mirrors `WorkerService` for embeddings. Lifecycle:

1. Polls `context_summary WHERE status = 'pending'` (configurable
   interval, default 5s).
2. For each pending row:
   - Load the parent raw row.
   - Load the full transcript from FileStore via `content_uri`.
   - Build the prompt (§7.3).
   - Call LLM (`SummarizerClient`, §8).
   - On success: write summary text to summary row's `content`; set
     `context_summary.status='done'`, `summarized_at=now`,
     `attempts += 1`.
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

New `SummarizerConfig` BaseModel in `config.py`, mirrors
`EmbedderConfig` exactly: same fields, same defaults logic via
`model_validator(mode="after")`, same `extra="forbid"` strictness.
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
   regenerates with the new transcript.

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

### 11.2 Embed pipeline interaction with summary row

Summary row has `content` (the summary text). Does the existing embed
pipeline embed it? **Default: yes** — it's a `kind=MEMORY` row, and
the embed pipeline embeds everything with non-empty content. This
gives hybrid search access to summary semantics, which is the whole
point.

But this means summary rows have **two** async dependencies: summary
generation (worker) + embedding (existing pipeline). Order:

1. Hook writes raw + empty summary + `context_summary(pending)`.
2. Existing embed pipeline sees raw row with content → embeds slim
   version.
3. Summary worker fills summary content.
4. Existing embed pipeline sees summary row now has content → embeds
   it.

Steps 2 and 3-4 are independent. Step 4 is automatic (embed pipeline
re-scans). No coordination needed.

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
- **Scope check:** single subsystem (auto-ingest), single source
  (Claude Code), single new table. Fits one plan.
- **Ambiguity check:** "non-git CWD" is fully specified (§5.4); "hook
  failure" is fully specified (§4.3); "summary failure" is fully
  specified (§7.4). The two genuinely open items (worker extend-vs-
  new, default model) are flagged with defaults.
