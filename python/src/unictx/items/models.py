"""Domain types for items + projects.

Ports Go's internal/domain/context.go and project.go. The Go struct
field layout is authoritative — every Go field has a Python counterpart.

Hybrid naming convention: snake_case in Python (PEP 8) for field names,
even though Go uses CamelCase. Dataclass field order matches Go struct
declaration order to ease cross-source review.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from unictx.items.errors import ItemValidationError

# Max byte length stored inline in ContextItem.content.
# Mirrors Go's ContentInlineLimit.
CONTENT_INLINE_LIMIT = 4 * 1024


class Scope(StrEnum):
    """Scope of a ContextItem. Values match Go's Scope* constants."""

    USER = "user"
    PROJECT = "project"
    GLOBAL = "global"


class Kind(StrEnum):
    """Kind of content. Values match Go's Kind* constants."""

    NOTE = "note"
    EXCERPT = "excerpt"
    LINK = "link"
    DOC = "doc"
    CONVERSATION_MSG = "conversation_msg"
    MEMORY = "memory"
    FILE = "file"


class Source(StrEnum):
    """Source of a ContextItem. Values match Go's Source* constants."""

    MANUAL = "manual"
    AGENT = "agent"
    SYNC = "sync"
    IMPORT = "import"
    WEBHOOK = "webhook"


class Visibility(StrEnum):
    """Visibility of a ContextItem. Values match Go's Visibility* constants."""

    PRIVATE = "private"
    PROJECT = "project"
    PUBLIC = "public"


@dataclass(slots=True)
class NewItemParams:
    """Optional fields for NewContextItem.

    Mirrors Go's NewItemParams struct. All defaults are empty strings
    so callers can omit fields they don't need; validation runs in
    NewContextItem via _validate_combination.
    """

    owner_user_id: str = ""
    project_id: str = ""
    agent_id: str = ""


@dataclass(slots=True)
class ContextItem:
    """Unified entity for all knowledge in the system.

    Field order matches Go's struct declaration for cross-source review.
    Some fields (title, summary, content, content_uri, ...) are empty by
    default — they are populated by the ingest pipeline, not by
    NewContextItem. NewContextItem only sets the identity/scope fields
    plus defaults (Visibility=PRIVATE, Confidence=1.0, Tags=[],
    SourceMeta={}, Version=1).
    """

    # Identity & graph
    id: str = ""
    scope: Scope = Scope.USER
    kind: Kind = Kind.NOTE
    source: Source = Source.MANUAL
    owner_user_id: str = ""
    project_id: str = ""
    agent_id: str = ""
    conversation_id: str = ""
    parent_id: str = ""

    # Content
    title: str = ""
    summary: str = ""
    content: str = ""  # inline, <= CONTENT_INLINE_LIMIT bytes
    content_uri: str = ""
    content_mime: str = ""
    content_hash: str = ""
    language: str = ""

    # Metadata
    tags: list[str] = field(default_factory=list)
    source_meta: dict[str, Any] = field(default_factory=dict)
    visibility: Visibility = Visibility.PRIVATE
    confidence: float = 1.0

    # Bookkeeping
    word_count: int = 0
    any_embedding: int = 0  # 0 or 1; always 0 in Plan 1
    created_at: int = 0  # unix timestamp
    updated_at: int = 0  # unix timestamp
    version: int = 1


@dataclass(slots=True)
class Project:
    """Project entity. Mirrors Go's domain.Project."""

    id: str = ""
    name: str = ""
    path: str = ""
    description: str = ""
    created_at: int = 0  # unix timestamp
    updated_at: int = 0  # unix timestamp


def new_project(name: str, path: str, description: str) -> Project:
    """Construct a Project with a fresh uuid7 id and timestamps.

    Mirrors Go's NewProject. Raises ItemValidationError if name is empty
    (Go returns ErrValidation-wrapped error; we surface the same way
    NewContextItem does, via the shared validation error type).
    """
    if not name:
        raise ItemValidationError("project name required")
    now = _now_unix()
    return Project(
        id=str(uuid.uuid7()),
        name=name,
        path=path,
        description=description,
        created_at=now,
        updated_at=now,
    )


def new_context_item(
    scope: Scope,
    kind: Kind,
    source: Source,
    params: NewItemParams,
    *,
    title: str = "",
    summary: str = "",
    content: str = "",
    content_uri: str = "",
    content_mime: str = "",
    content_hash: str = "",
    language: str = "",
    tags: list[str] | None = None,
    source_meta: dict[str, Any] | None = None,
    conversation_id: str = "",
    parent_id: str = "",
) -> ContextItem:
    """Construct a ContextItem with scope/kind/source invariants enforced.

    Mirrors Go's NewContextItem. Validates the combination via
    _validate_combination (raises ItemValidationError on failure), then
    sets defaults: Visibility=PRIVATE, Confidence=1.0, Tags=[],
    SourceMeta={}, Version=1, timestamps to int UTC now, id to uuid7.

    Optional content fields (title, summary, content, ...) are accepted
    as keyword args so callers can construct a fully-populated item in
    one call. Go's NewContextItem leaves these blank and the ingest
    pipeline fills them; we expose them as kwargs to ease testing —
    pass nothing to get Go-equivalent behavior.
    """
    _validate_combination(scope, kind, source, params)
    now = _now_unix()
    return ContextItem(
        id=str(uuid.uuid7()),
        scope=scope,
        kind=kind,
        source=source,
        owner_user_id=params.owner_user_id,
        project_id=params.project_id,
        agent_id=params.agent_id,
        conversation_id=conversation_id,
        parent_id=parent_id,
        title=title,
        summary=summary,
        content=content,
        content_uri=content_uri,
        content_mime=content_mime,
        content_hash=content_hash,
        language=language,
        tags=list(tags) if tags is not None else [],
        source_meta=dict(source_meta) if source_meta is not None else {},
        visibility=Visibility.PRIVATE,
        confidence=1.0,
        word_count=count_words(content),
        any_embedding=0,
        created_at=now,
        updated_at=now,
        version=1,
    )


def _validate_combination(
    scope: Scope,
    kind: Kind,
    source: Source,
    params: NewItemParams,
) -> None:
    """Port of Go's validateCombination.

    Raises ItemValidationError on the first violated rule. Rules are
    enumerated in the same order as Go to ease review.
    """
    if scope == Scope.GLOBAL:
        if params.owner_user_id:
            raise ItemValidationError("global must not have owner")
        if params.project_id:
            raise ItemValidationError("global must not have project")
    elif scope == Scope.USER:
        if not params.owner_user_id:
            raise ItemValidationError("user scope requires owner")
        if params.project_id:
            raise ItemValidationError("user scope must not have project (use project scope)")
    elif scope == Scope.PROJECT:
        if not params.project_id:
            raise ItemValidationError("project scope requires project_id")
    else:
        raise ItemValidationError(f"unknown scope {scope!r}")

    if kind == Kind.MEMORY:
        if scope != Scope.PROJECT:
            raise ItemValidationError("memory kind requires project scope")
        if source not in (Source.AGENT, Source.SYNC):
            raise ItemValidationError("memory kind requires source=agent or sync")
    if kind == Kind.CONVERSATION_MSG:
        if scope != Scope.PROJECT:
            raise ItemValidationError("conversation_msg kind requires project scope")
        if source not in (Source.AGENT, Source.SYNC):
            raise ItemValidationError("conversation_msg kind requires source=agent or sync")


def count_words(text: str) -> int:
    """Port of Go's countWords (in internal/service/ingest.go).

    Algorithm:
      - whitespace rune ends the current word
      - CJK ideograph counts as one word on its own (no inWord state)
      - any other non-space rune: counts as start-of-word if not already
        inside a word

    Binding parity note (plan §Python Conventions): Go's implementation
    undercounts CJK relative to proper word segmentation — every CJK
    ideograph is one "word", so a multi-character Chinese word is
    counted as N words. We preserve this for parity. Do NOT swap in
    a proper word segmenter without a separate plan discussion.
    """
    n = 0
    in_word = False
    for r in text:
        if r.isspace():
            in_word = False
            continue
        if _is_cjk(r):
            in_word = False
            n += 1
            continue
        if not in_word:
            n += 1
            in_word = True
    return n


def _is_cjk(r: str) -> bool:
    """Port of Go's isCJK. Expects a single-character string."""
    cp = ord(r)
    return (
        0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF  # CJK Extension A
        or 0x3040 <= cp <= 0x30FF  # Hiragana + Katakana
        or 0xAC00 <= cp <= 0xD7AF  # Hangul Syllables
        or 0xF900 <= cp <= 0xFAFF  # CJK Compatibility Ideographs
        or 0x31C0 <= cp <= 0x31EF  # CJK Strokes
    )


def _now_unix() -> int:
    """Current UTC time as integer unix timestamp."""
    return int(datetime.now(UTC).timestamp())


# ---------------------------------------------------------------------------
# Access direction (P1 trust boundary)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AccessGrant:
    """One row of the ``access_grant`` table.

    A grant EXTENDS the default visible_scopes for an actor — it can
    only widen, never narrow, the default set. See migration 0005 and
    :func:`visible_scopes`.

    Fields:
        as_scope: the actor's identity scope this grant applies to.
            Only ``project`` / ``global`` — ``user`` is the innermost
            layer and sees everything by default, so a user grant is
            meaningless.
        project_id: empty string = the grant applies to ALL projects
            acting with this as_scope; non-empty = only that specific
            project. (The DB stores NULL for "all"; this dataclass uses
            "" to stay consistent with the rest of the domain's
            string-typed optional fields.)
        target_scope: the scope whose data the actor may now see
            (``user`` | ``project`` | ``global``).
        reason: optional human-readable justification (audit trail). The
            DB stores it verbatim; ``visible_scopes`` ignores it. Empty
            by default for grants created without an explicit reason.
    """

    as_scope: Scope = Scope.GLOBAL
    project_id: str = ""
    target_scope: Scope = Scope.GLOBAL
    reason: str = ""


# Default visible scope sets per access identity. The access identity is
# "who is asking"; the innermost layer (user) sees the most, the
# outermost (global) sees the least. This is the unbreakable floor that
# visible_scopes() returns before grants widen it.
#
# project_id isolation is NOT encoded here: PROJECT actors see the whole
# ``project`` scope dimension, but the caller enforces (at the SQL /
# filter layer) that a PROJECT actor only sees rows whose project_id
# matches the actor's. global rows are never project-scoped.
_DEFAULT_VISIBLE: dict[Scope, list[Scope]] = {
    Scope.USER: [Scope.USER, Scope.PROJECT, Scope.GLOBAL],
    Scope.PROJECT: [Scope.PROJECT, Scope.GLOBAL],
    Scope.GLOBAL: [Scope.GLOBAL],
}


def visible_scopes(
    as_scope: Scope,
    grants: list[AccessGrant] | None = None,
) -> list[Scope]:
    """Return the visible scope set for an access identity, widened by grants.

    Pure function — the single source of truth for the default access
    floor. No IO; trivially unit-testable.

    Default rule (the unbreakable floor; an access identity further out
    sees a subset of what a more-inner identity sees)::

        USER    -> [user, project, global]   # innermost, sees all
        PROJECT -> [project, global]          # cannot see user's private data
        GLOBAL  -> [global]                   # outermost, sees only global

    Grants can only widen: any grant whose ``as_scope`` matches is
    applied, adding its ``target_scope`` to the visible set. A grant
    whose ``project_id`` is set is still applied here — project-level
    restriction is the caller's responsibility (the caller knows which
    project it is acting as, so it filters the grant list before calling
    or passes only the relevant grants).

    Returns scopes in :class:`Scope` declaration order (USER, PROJECT,
    GLOBAL), deduplicated. This stable ordering keeps test assertions
    deterministic.

    Note on PROJECT isolation: this function deliberately returns the
    full ``project`` scope dimension for a PROJECT actor. The
    project-to-project isolation (a PROJECT actor only sees rows whose
    ``project_id`` matches its own) is enforced downstream via SQL
    predicates / filter acceptance, not here — because that restriction
    is row-level (depends on each item's project_id), not scope-level.
    """
    # Start from the default floor. Unknown as_scope falls back to the
    # most restrictive (global) — fail-closed, never leak.
    visible = list(_DEFAULT_VISIBLE.get(as_scope, _DEFAULT_VISIBLE[Scope.GLOBAL]))

    if not grants:
        return _dedup_scopes(visible)

    seen = set(visible)
    for grant in grants:
        # A grant only applies to actors matching its as_scope. We do
        # not filter by project_id here — the caller passes only the
        # grants relevant to the acting project (or passes all and
        # accepts that a project-scoped grant widens this actor only
        # when the actor's project matches, which the caller enforces
        # by pre-filtering). Keeping this function pure + project-blind
        # means the rule is testable without constructing projects.
        if grant.as_scope != as_scope:
            continue
        if grant.target_scope not in seen:
            seen.add(grant.target_scope)
            visible.append(grant.target_scope)

    return _dedup_scopes(visible)


def _dedup_scopes(scopes: list[Scope]) -> list[Scope]:
    """De-duplicate scopes, preserving the :class:`Scope` declaration order.

    The declaration order (USER, PROJECT, GLOBAL) is the canonical order
    returned by visible_scopes; this keeps the output stable regardless
    of how grants were ordered in the input list.
    """
    seen: set[Scope] = set()
    out: list[Scope] = []
    for scope in scopes:
        if scope in seen:
            continue
        seen.add(scope)
        out.append(scope)
    # Re-sort by declaration order so output is deterministic regardless
    # of grant insertion order.
    order = {Scope.USER: 0, Scope.PROJECT: 1, Scope.GLOBAL: 2}
    out.sort(key=lambda s: order[s])
    return out
