# uni-context Vision

> Status: **North-star reference.** Not a spec, not a plan. Every
> implementation plan should link back here so the why stays anchored.
>
> Last refined: 2026-07-01.

## One-line summary

**A local-first, unlimited-scope context management tool.** Local
execution is the only hard constraint; everything else (data shapes,
ingest sources, retrieval methods, storage backends, AI assistance) is
designed to be extensible without rewriting the core.

## Why local-first

Local-first is not a deployment detail — it is the trust foundation.
Several design choices only make sense under it:

- The tool can be granted **broad local-data management authority**
  after explicit user consent. There is no multi-tenant isolation
  problem to solve, no cloud egress policy to satisfy.
- "Implicit habits" (tech-stack preference, system-config patterns —
  see [Global scope](#global-scope-the-ambient-layer)) can be recorded
  silently because the data never leaves the user's machine.
- The user's mental model is "this is mine, on this machine" — not
  "this is uploaded to a service."

## The three scope layers

Scope is the **access-direction** axis. The current implementation
already enforces it via `visible_scopes()` + `SearchService._converge()`
(see `python/src/unictx/items/models.py`, `python/src/unictx/search/
service.py`). P1 (landed 2026-07-01) is the trust boundary that makes
later auto-ingest safe.

| Scope | Typical source | Privacy | Access direction (default, no grants) |
|-------|---------------|---------|---------------------------------------|
| **user** | Mostly **manual** entry by the user. The richest management operations apply here. | Highest — innermost layer | Sees `{user, project, global}` |
| **project** | Mostly **automatic** ingest from Code Agent Sessions via hooks. Domain-bound; valuable when the project is active; serves Agents working in that project. | Mid | Sees `{project, global}` + own `project_id` only (project-to-project isolation enforced at row level) |
| **global** | Both auto and manual. Ambient preferences (passive) + system-wide rules (active). | Lowest — outermost layer; shared | Sees `{global}` only |

Access direction: `user → project → global`, `user → global`. The
innermost layer (user) sees everything by default; outer layers see
strictly less. **Grants only widen, never narrow.**

### Global scope: the ambient layer

Global has two distinct natures that future work must keep separable:

- **Passive features** — silently recorded implicit habits: tech-stack
  preferences, system-config patterns, terminal shortcuts. Append-mostly,
  AI-inferred, possibly wrong, low `confidence`. The data describes the
  user back to themselves.
- **Active directives** — explicit rules an Agent must follow ("use
  brew to install apps", "prefer this lint config"). Authored, high
  `confidence`, revocable.

These have different lifecycles. The current `Visibility` enum
(`private | project | public`) is **dormant** and may be the natural
carrier for the passive/active distinction — but this is not yet
decided. See [Open questions](#open-questions).

## Two orthogonal classification axes

A `ContextItem` is classified along **two independent axes**, not one.
This was decided 2026-07-01: prior drafts conflated them, which would
have blocked expressions like "this PDF is a doc **and** a directive"
or "this conversation_msg is a memory **and** a relation target."

### Axis 1 — `Kind` (content shape)

Already shipped. Describes **what the content physically is**.

```
NOTE | EXCERPT | LINK | DOC | CONVERSATION_MSG | MEMORY | FILE
```

A PDF is `DOC`. A pasted snippet is `EXCERPT`. A raw file attachment is
`FILE`. `Kind` determines which extractor / hydrator / viewer handles
the row.

### Axis 2 — `Role` (semantic purpose)

**Not yet implemented.** Describes **how the content is used** by
Agents and the retrieval pipeline.

```
memory     — captured from conversations; recall-oriented
document   — a source artifact; cite-oriented
entity     — an extracted object (person, library, concept); graph node
relation   — a connection between entities; graph edge
directive  — a rule Agents must follow; constraint-oriented
```

A single row carries **one `Kind` and one `Role`**, independently. The
`MEMORY` value on `Kind` and `memory` value on `Role` look similar but
mean different things: `Kind=MEMORY` says "this row's bytes look like a
chat utterance"; `Role=memory` says "this row is used for recall."

**Decision deferred to implementation:** `Role` may end up as a new
enum field on `ContextItem`, or as a separate join table
(`item_id × role`) so one item can carry multiple roles. The join
option trades query simplicity for tagging flexibility; the enum option
trades flexibility for query simplicity. Pick when the first
role-consuming feature lands.

## Ingest: auto vs manual

The vision distinguishes **how content enters**, not just what it is:

- **Manual** — user types/pastes/attaches via CLI (`user note add`,
  attachments). Source enum: `MANUAL`, `IMPORT`.
- **Automatic** — Agents/hooks write on the user's behalf. Source enum:
  `AGENT`, `SYNC`, `WEBHOOK`.

The `Source` enum (already on `ContextItem`) is the seam. Auto-ingest
paths are **not yet wired** (P3 work). When they land, two guarantees
must hold:

1. Every auto-ingested row carries `source != MANUAL` so audit tools
   can filter user-authored content from agent-authored content.
2. Auto-ingest respects the P1 access boundary — a project Agent can
   only write to its own `project` scope, never to `user` scope,
   unless explicitly granted. (Grant widening applies to writes too,
   not just reads. This is **open**: P1 only enforces read-side
   convergence.)

## Retrieval: hybrid by default

Already shipped: FTS (BM25) + vector KNN (vec0), fused via RRF (k=60),
with four degradation paths (embed fail, vector timeout, fts timeout,
no embedder) all converging through the single `SearchService.search()`
entry point so the access boundary cannot be bypassed by a degraded
leg.

Future: RAG over retrieved context, re-ranking, graph traversal for
entity/relation queries. None of these change the access boundary —
they layer on top of the converged `SearchRequest`.

## Storage: pluggable, but not premature

Current: SQLite (rows + FTS5) + sqlite-vec (vectors) in a single
`unictx.db` file. The `port.ContextRepo` Protocol already abstracts
row storage; `port.Searcher` and `port.VectorStore` abstract retrieval.

**Graph storage is an independent extension, not a unification.** When
entity/relation features land, a new `RelationRepo` Protocol will sit
**alongside** `ContextRepo`, not replace it. Forcing edges into
`ContextRepo.list()` would distort the row-oriented interface. The
two protocols will share the same DB file initially (SQLite is
perfectly capable of graph-shaped queries via recursive CTEs) but
remain independent — a future migration to a real graph backend would
only touch `RelationRepo` impls.

This decision was made 2026-07-01: **graph storage is planned but not
in scope for any current phase.**

## Multi-format ingest

| Format | Status |
|--------|--------|
| Plain text | ✅ Manual + auto paths |
| PDF | ✅ PyMuPDF (default) + shell (`pdftotext`) + http (service) |
| Word (.docx) | ❌ Future |
| Excel (.xlsx) | ❌ Future |
| Image | ❌ Future (likely: vision model → text → DOC, with the binary preserved in FileStore) |
| Audio | ❌ Future (likely: transcription → DOC, with binary preserved) |
| Other binary | ❌ Future (store as FILE, no extraction) |

The extractor factory pattern (`python/src/unictx/pdf/factory.py`) is
the template — a new format adds one extractor + one factory branch,
not a refactor.

## Living context: AI-assisted tidying

Context is not append-only dead storage. The tool will eventually run
background AI jobs that:

- Compact long conversations into memory summaries.
- Extract entities/relations from documents.
- Surface stale directives for review.
- Deduplicate semantically-equivalent notes.

**Three fields already exist to support this and are currently
under-used:**

| Field | Current state | Role in the AI-tidying future |
|-------|--------------|------------------------------|
| `ContextItem.confidence` | Hard-coded to `1.0` everywhere | AI-inferred content (`global` passive features, extracted entities) should write `< 1.0`. Retrieval can downgrade-rank low-confidence rows. |
| `ContextItem.version` | Starts at `1`, never bumped | AI compaction/summarization bumps version; old version retained for rollback. |
| `ContextItem.source_meta` | Always `{}` | Provenance: which model generated this, from which source rows, at what time. Required for audit. |

Design rule for any future AI write path: **never mutate in place
without bumping `version`**. The user must always be able to roll back
to pre-AI state.

## External interfaces

| Interface | Status | Notes |
|-----------|--------|-------|
| CLI | ✅ Primary | Typer-based. Every command supports `--json`. |
| MCP server | ❌ Deferred | Not urgent — Agents via CLI is a common pattern. Will land later. |
| Webhook | ❌ Deferred | Auto-ingest seam (`Source.WEBHOOK`) already exists. |

## Open questions

Decisions explicitly **not** made yet; future plans must resolve them
in scope:

1. **`Visibility` enum's job.** Currently dormant. Is it the carrier
   for the global-scope passive/active distinction (authority level),
   or for cross-scope sharing semantics (who can read this specific
   row)? These are different questions; one enum cannot answer both.
2. **Write-side access boundary.** P1 enforces read-side convergence.
   Auto-ingest will need a write-side boundary (a project Agent cannot
   write to `user` scope). Is this the same grant table (read+write
   widened together) or a separate one?
3. **`Role` storage shape.** Enum field on `ContextItem` vs join table
   (`item_id × role`). Decided at first role-consuming feature.
4. **Grant uniqueness.** `access_grant` table currently permits
   duplicates (intentional — see `AccessRepo.grant` docstring). When
   management gets noisy, decide if a unique constraint on
   `(as_scope, project_id, target_scope)` is wanted.
5. **TTL / decay for global passive features.** Tech-stack preferences
   go stale. No current mechanism. Likely a janitor job, not a schema
   change.

## Phase map (where each piece of the vision lands)

| Vision element | Phase | Status |
|---------------|-------|--------|
| Scope layers + access direction | P1 | ✅ Shipped 2026-07-01 |
| Grant management CLI | P1.1 | ✅ Shipped 2026-07-01 (beyond original plan) |
| Auto-ingest from Agent sessions (project scope, hook-driven) | P3 | Planned — P1 unblocked it |
| Multi-format ingest (Word/Excel/image/audio) | later | Planned |
| `Role` axis + entity/relation graph | later | Planned — graph storage decision: independent `RelationRepo` |
| AI-assisted tidying (compaction, entity extraction) | later | Planned — `confidence`/`version`/`source_meta` already in place |
| MCP server | later | Deferred — CLI-first |
| Webhook ingest | later | Deferred — seam exists (`Source.WEBHOOK`) |

## How to use this document

- **When writing a plan:** open this doc first. Cite the section your
  plan advances. If a plan touches something not in this doc, the doc
  needs updating first.
- **When making an architectural call:** check [Open questions](#open-
  questions) — if your call resolves one, edit the doc to move it out
  of that section.
- **When the vision shifts:** edit this doc, don't append. The doc is
  the current truth, not a changelog. (Chronological history lives in
  `CHANGELOG.md`.)
