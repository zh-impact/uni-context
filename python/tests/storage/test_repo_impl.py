"""Tests for unictx.storage.repo_impl.ContextRepoImpl.

Ports the round-trip and edge-case scenarios that Go exercises in
``archive/go/internal/adapter/sqlite/repo_test.go`` plus the cursor-format
cross-check from the migration spike. Together these cover:

* All 7 Protocol methods (create/get/update/delete/list/next_cursor/reindex_fts)
* NULL → "" coercion via scan_item (mirrors Go's ``sql.NullString`` zero value)
* Cursor format byte-identical to Go's ``strconv.FormatInt(ts, 36)`` —
  verified by encoding authoritative Go outputs and asserting equality.
* Multi-statement reindex_fts SQL wrapped in an explicit BEGIN/COMMIT
  (the Go side runs both statements in one ``ExecContext`` call; Python's
  ``cursor.execute`` runs only one, so the explicit tx preserves atomicity).

The fixture ``migrated_db`` (tests/conftest.py) yields a fresh
``:memory:`` connection with all migrations applied. Each test gets its
own isolated DB — no cross-test coupling and no on-disk cleanup.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from unictx.items.errors import ItemNotFound
from unictx.items.models import (
    ContextItem,
    Kind,
    Scope,
    Source,
    Visibility,
)
from unictx.items.repo import ItemFilter
from unictx.storage.repo_impl import ContextRepoImpl, decode_cursor, encode_cursor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _full_item(
    *,
    id: str = "item-1",
    scope: Scope = Scope.USER,
    kind: Kind = Kind.NOTE,
    source: Source = Source.MANUAL,
    owner_user_id: str = "user-1",
    project_id: str = "",
    title: str = "Hello world",
    summary: str = "A summary",
    content: str = "Body content here",
    tags: list[str] | None = None,
    source_meta: dict[str, Any] | None = None,
    language: str = "en",
    created_at: int = 1_700_000_000,
    updated_at: int = 1_700_000_000,
    version: int = 1,
    any_embedding: int = 0,
    content_uri: str = "",
    content_mime: str = "text/plain",
    content_hash: str = "sha256:abc",
    agent_id: str = "agent-7",
    conversation_id: str = "conv-1",
    parent_id: str = "",
    confidence: float = 0.85,
    word_count: int = 3,
) -> ContextItem:
    """Build a fully-populated ContextItem with non-trivial field values.

    The round-trip test asserts every field comes back identical, so we
    avoid defaults that would mask a missing-column bug. Pass kwargs to
    override individual fields per-test.
    """
    return ContextItem(
        id=id,
        scope=scope,
        kind=kind,
        source=source,
        owner_user_id=owner_user_id,
        project_id=project_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        parent_id=parent_id,
        title=title,
        summary=summary,
        content=content,
        content_uri=content_uri,
        content_mime=content_mime,
        content_hash=content_hash,
        language=language,
        tags=tags if tags is not None else ["alpha", "beta"],
        source_meta=source_meta if source_meta is not None else {"k": "v", "n": 7},
        visibility=Visibility.PRIVATE,
        confidence=confidence,
        word_count=word_count,
        any_embedding=any_embedding,
        created_at=created_at,
        updated_at=updated_at,
        version=version,
    )


# ---------------------------------------------------------------------------
# Round-trip: create → get
# ---------------------------------------------------------------------------


def test_create_then_get_roundtrips_all_fields(migrated_db: sqlite3.Connection) -> None:
    """create() then get() must return an item with every field intact.

    Asserts all 24 ContextItem fields round-trip through JSON encoding
    (tags, source_meta), NULL storage (project_id=""), StrEnum round-trip
    (scope/kind/source/visibility), and timestamps.
    """
    repo = ContextRepoImpl(migrated_db)
    original = _full_item()
    repo.create(original)

    got = repo.get(original.id)

    # Identity & graph
    assert got.id == original.id
    assert got.scope == original.scope == Scope.USER
    assert got.kind == original.kind == Kind.NOTE
    assert got.source == original.source == Source.MANUAL
    assert got.owner_user_id == original.owner_user_id == "user-1"
    assert got.project_id == original.project_id == ""
    assert got.agent_id == original.agent_id == "agent-7"
    assert got.conversation_id == original.conversation_id == "conv-1"
    assert got.parent_id == original.parent_id == ""

    # Content
    assert got.title == original.title == "Hello world"
    assert got.summary == original.summary == "A summary"
    assert got.content == original.content == "Body content here"
    assert got.content_uri == original.content_uri == ""
    assert got.content_mime == original.content_mime == "text/plain"
    assert got.content_hash == original.content_hash == "sha256:abc"
    assert got.language == original.language == "en"

    # Metadata (JSON-decoded)
    assert got.tags == original.tags == ["alpha", "beta"]
    assert got.source_meta == original.source_meta == {"k": "v", "n": 7}
    assert got.visibility == original.visibility == Visibility.PRIVATE
    assert got.confidence == original.confidence == 0.85

    # Bookkeeping
    assert got.word_count == original.word_count == 3
    assert got.any_embedding == original.any_embedding == 0
    assert got.created_at == original.created_at == 1_700_000_000
    assert got.updated_at == original.updated_at == 1_700_000_000
    assert got.version == original.version == 1


def test_create_stores_null_for_empty_optional_strings(migrated_db: sqlite3.Connection) -> None:
    """Empty optional strings become SQL NULL, not ''.

    Mirrors Go's ``nullable()`` helper. Verified by reading the raw row
    back through a no-row_factory cursor — we want to see NULL, not the
    row_factory's coerced "".
    """
    repo = ContextRepoImpl(migrated_db)
    item = _full_item(
        id="nulls",
        owner_user_id="owner",  # required for USER scope
        project_id="",
        agent_id="",
        conversation_id="",
        parent_id="",
        content_uri="",
        content_mime="",
        content_hash="",
        language="",
    )
    repo.create(item)

    # Bypass the row_factory to inspect raw SQL NULL.
    cur = migrated_db.execute(
        "SELECT project_id, agent_id, conversation_id, parent_id, "
        "content_uri, content_mime, content_hash, language "
        "FROM context_item WHERE id = ?",
        (item.id,),
    )
    cur.row_factory = None
    row = cur.fetchone()
    assert row is not None
    # Every column must be NULL in storage — the repo's nullable() helper
    # converted "" → None before INSERT.
    assert row == (None, None, None, None, None, None, None, None)


# ---------------------------------------------------------------------------
# Get missing
# ---------------------------------------------------------------------------


def test_get_missing_raises_item_not_found(migrated_db: sqlite3.Connection) -> None:
    """get() on an unknown id raises ItemNotFound with the right item_id."""
    repo = ContextRepoImpl(migrated_db)
    with pytest.raises(ItemNotFound) as exc_info:
        repo.get("does-not-exist")
    assert exc_info.value.item_id == "does-not-exist"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_increments_version_and_returns_mutated_item(
    migrated_db: sqlite3.Connection,
) -> None:
    """update() returns the same instance with version+1 and preserves updated_at.

    Mirrors Go's repo.go:76-78 — Update increments version but does NOT
    refresh updated_at (Go only normalizes tz; Python is tz-naive). The
    service layer is responsible for setting updated_at before calling
    update. The returned item carries the same updated_at the caller
    supplied.
    """
    repo = ContextRepoImpl(migrated_db)
    # Distinct updated_at (newer than created_at, as a real service would
    # set before calling update). update() must preserve this value
    # verbatim — no refresh, no tz touch.
    original = _full_item(
        id="upd-1",
        version=1,
        created_at=1_700_000_000,
        updated_at=1_700_000_042,
    )
    repo.create(original)
    original_updated_at = original.updated_at

    # Mutate a content field then update.
    original.title = "Updated title"
    updated = repo.update(original)

    # Same instance — Go's mutation-order cosmetic preserved.
    assert updated is original
    assert updated.version == 2
    assert updated.title == "Updated title"
    # updated_at is preserved verbatim — repo must NOT refresh it.
    assert updated.updated_at == original_updated_at

    # Re-fetch and confirm the DB has the new values.
    refetched = repo.get(original.id)
    assert refetched.version == 2
    assert refetched.title == "Updated title"
    assert refetched.updated_at == updated.updated_at == original_updated_at


def test_update_missing_raises_item_not_found(migrated_db: sqlite3.Connection) -> None:
    """update() on an unknown id raises ItemNotFound (Go's RowsAffected==0 check)."""
    repo = ContextRepoImpl(migrated_db)
    ghost = _full_item(id="ghost", version=1)
    with pytest.raises(ItemNotFound) as exc_info:
        repo.update(ghost)
    assert exc_info.value.item_id == "ghost"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_happy_path(migrated_db: sqlite3.Connection) -> None:
    """delete() removes the row; subsequent get() raises ItemNotFound."""
    repo = ContextRepoImpl(migrated_db)
    item = _full_item(id="del-1")
    repo.create(item)
    repo.delete(item.id)
    with pytest.raises(ItemNotFound):
        repo.get(item.id)


def test_delete_missing_raises_item_not_found(migrated_db: sqlite3.Connection) -> None:
    """delete() on an unknown id raises ItemNotFound (Go's RowsAffected==0 check)."""
    repo = ContextRepoImpl(migrated_db)
    with pytest.raises(ItemNotFound) as exc_info:
        repo.delete("never-existed")
    assert exc_info.value.item_id == "never-existed"


# ---------------------------------------------------------------------------
# List + pagination
# ---------------------------------------------------------------------------


def test_list_paginates_via_cursor(migrated_db: sqlite3.Connection) -> None:
    """list() returns limit rows + a cursor that resumes the next page.

    Creates 5 items with controlled created_at timestamps so the ORDER BY
    is deterministic. Pages through with limit=2, asserts every item is
    visited exactly once and the final cursor is empty.
    """
    repo = ContextRepoImpl(migrated_db)
    # 5 items with strictly-increasing created_at — ORDER BY created_at DESC
    # yields item-5, item-4, ..., item-1.
    for i in range(5):
        item = _full_item(
            id=f"page-{i}",
            owner_user_id="u-page",
            created_at=1_700_000_000 + i,
            updated_at=1_700_000_000 + i,
        )
        repo.create(item)

    seen: list[str] = []
    cursor = ""
    # Iterate up to 5 pages (safety against infinite loop).
    for _ in range(5):
        rows, next_cursor = repo.list(ItemFilter(owner_user_id="u-page", limit=2, cursor=cursor))
        seen.extend(r.id for r in rows)
        cursor = next_cursor
        if not cursor:
            break

    # ORDER BY created_at DESC → page-4 first, page-0 last.
    assert seen == ["page-4", "page-3", "page-2", "page-1", "page-0"]


def test_list_returns_empty_cursor_when_page_complete(
    migrated_db: sqlite3.Connection,
) -> None:
    """If rows fit in one page, next_cursor is the empty string."""
    repo = ContextRepoImpl(migrated_db)
    repo.create(_full_item(id="only", owner_user_id="u-one"))

    rows, next_cursor = repo.list(ItemFilter(owner_user_id="u-one", limit=10))
    assert [r.id for r in rows] == ["only"]
    assert next_cursor == ""


def test_list_filters_by_scope(migrated_db: sqlite3.Connection) -> None:
    """scopes filter restricts to items whose scope is in the supplied set."""
    repo = ContextRepoImpl(migrated_db)
    repo.create(_full_item(id="u", scope=Scope.USER, owner_user_id="owner"))
    # Project-scope item requires project_id; the schema has an FK on
    # project_id → project(id), so seed the project row first.
    migrated_db.execute(
        "INSERT INTO project (id, name, path, description, created_at, updated_at) "
        "VALUES ('proj-1', 'proj-1', '', '', 1700000000, 1700000000)"
    )
    repo.create(
        _full_item(
            id="p",
            scope=Scope.PROJECT,
            owner_user_id="",
            project_id="proj-1",
        )
    )
    # And a global-scope item (no owner, no project).
    repo.create(
        _full_item(
            id="g",
            scope=Scope.GLOBAL,
            owner_user_id="",
            project_id="",
        )
    )

    rows, _ = repo.list(ItemFilter(scopes=[Scope.USER], limit=10))
    assert {r.id for r in rows} == {"u"}

    rows, _ = repo.list(ItemFilter(scopes=[Scope.PROJECT], limit=10))
    assert {r.id for r in rows} == {"p"}

    rows, _ = repo.list(ItemFilter(scopes=[Scope.GLOBAL], limit=10))
    assert {r.id for r in rows} == {"g"}

    # Multiple scopes → union.
    rows, _ = repo.list(ItemFilter(scopes=[Scope.USER, Scope.PROJECT], limit=10))
    assert {r.id for r in rows} == {"u", "p"}


def test_list_project_isolation_restricts_to_own_project(
    migrated_db: sqlite3.Connection,
) -> None:
    """P1: a PROJECT actor sees only its own project_id's project rows + global.

    Project P cannot see project Q's items. Global rows stay shared.
    Mirrors the SearchService project-isolation rule, but enforced at
    the SQL layer via the (project_id=? OR scope='global') predicate.
    """
    repo = ContextRepoImpl(migrated_db)
    # Two projects, each with its own item, plus a shared global item.
    migrated_db.execute(
        "INSERT INTO project (id, name, path, description, created_at, updated_at) "
        "VALUES ('P', 'P', '', '', 1700000000, 1700000000),"
        "       ('Q', 'Q', '', '', 1700000000, 1700000000)"
    )
    repo.create(_full_item(id="mine", scope=Scope.PROJECT, owner_user_id="", project_id="P"))
    repo.create(_full_item(id="theirs", scope=Scope.PROJECT, owner_user_id="", project_id="Q"))
    repo.create(_full_item(id="shared", scope=Scope.GLOBAL, owner_user_id="", project_id=""))

    # Project P acting: sees its own + global, NOT Q's.
    rows, _ = repo.list(
        ItemFilter(
            scopes=[Scope.PROJECT, Scope.GLOBAL],
            as_scope=Scope.PROJECT,
            as_project_id="P",
            limit=10,
        )
    )
    assert {r.id for r in rows} == {"mine", "shared"}
    assert "theirs" not in {r.id for r in rows}

    # Project Q acting: sees its own + global, NOT P's.
    rows, _ = repo.list(
        ItemFilter(
            scopes=[Scope.PROJECT, Scope.GLOBAL],
            as_scope=Scope.PROJECT,
            as_project_id="Q",
            limit=10,
        )
    )
    assert {r.id for r in rows} == {"theirs", "shared"}


def test_list_user_actor_no_project_isolation(migrated_db: sqlite3.Connection) -> None:
    """P1: a USER actor (default) applies no project_id predicate.

    USER sees all projects' project rows when scopes include PROJECT —
    no isolation. Confirms the predicate only fires for as_scope=PROJECT.
    """
    repo = ContextRepoImpl(migrated_db)
    migrated_db.execute(
        "INSERT INTO project (id, name, path, description, created_at, updated_at) "
        "VALUES ('P', 'P', '', '', 1700000000, 1700000000),"
        "       ('Q', 'Q', '', '', 1700000000, 1700000000)"
    )
    repo.create(_full_item(id="pp", scope=Scope.PROJECT, owner_user_id="", project_id="P"))
    repo.create(_full_item(id="qq", scope=Scope.PROJECT, owner_user_id="", project_id="Q"))

    rows, _ = repo.list(
        ItemFilter(scopes=[Scope.PROJECT], as_scope=Scope.USER, limit=10)
    )
    # USER sees both projects — no isolation.
    assert {r.id for r in rows} == {"pp", "qq"}


def test_list_filters_by_kind(migrated_db: sqlite3.Connection) -> None:
    """kinds filter restricts to items whose kind is in the supplied set."""
    repo = ContextRepoImpl(migrated_db)
    repo.create(_full_item(id="note", kind=Kind.NOTE, owner_user_id="owner"))
    repo.create(_full_item(id="link", kind=Kind.LINK, owner_user_id="owner"))

    rows, _ = repo.list(ItemFilter(kinds=[Kind.NOTE], limit=10))
    assert {r.id for r in rows} == {"note"}


def test_list_filters_by_owner_user_id(migrated_db: sqlite3.Connection) -> None:
    """owner_user_id filter is an exact-match predicate."""
    repo = ContextRepoImpl(migrated_db)
    repo.create(_full_item(id="alice", owner_user_id="alice"))
    repo.create(_full_item(id="bob", owner_user_id="bob"))

    rows, _ = repo.list(ItemFilter(owner_user_id="alice", limit=10))
    assert {r.id for r in rows} == {"alice"}


def test_list_filters_by_tags_or_semantics(migrated_db: sqlite3.Connection) -> None:
    """tags filter matches items carrying ANY of the requested tags."""
    repo = ContextRepoImpl(migrated_db)
    repo.create(_full_item(id="ab", tags=["alpha", "beta"], owner_user_id="owner"))
    repo.create(_full_item(id="bc", tags=["beta", "gamma"], owner_user_id="owner"))
    repo.create(_full_item(id="gz", tags=["gamma", "zeta"], owner_user_id="owner"))

    # alpha → only ab
    rows, _ = repo.list(ItemFilter(tags=["alpha"], limit=10))
    assert {r.id for r in rows} == {"ab"}

    # alpha OR zeta → ab + gz
    rows, _ = repo.list(ItemFilter(tags=["alpha", "zeta"], limit=10))
    assert {r.id for r in rows} == {"ab", "gz"}


def test_list_filters_by_any_embedding(migrated_db: sqlite3.Connection) -> None:
    """any_embedding tri-state: None=no filter, 0=unembedded, 1=embedded."""
    repo = ContextRepoImpl(migrated_db)
    repo.create(_full_item(id="no-emb", any_embedding=0, owner_user_id="owner"))
    repo.create(_full_item(id="emb", any_embedding=1, owner_user_id="owner"))

    # No filter → both
    rows, _ = repo.list(ItemFilter(limit=10))
    assert {r.id for r in rows} == {"no-emb", "emb"}

    # 0 → only unembedded
    rows, _ = repo.list(ItemFilter(any_embedding=0, limit=10))
    assert {r.id for r in rows} == {"no-emb"}

    # 1 → only embedded
    rows, _ = repo.list(ItemFilter(any_embedding=1, limit=10))
    assert {r.id for r in rows} == {"emb"}


def test_list_filters_by_not_done_for_model(migrated_db: sqlite3.Connection) -> None:
    """not_done_for_model excludes items with a status='done' row.

    The EmbeddingRepo is Task 2.6, so we insert the status row directly
    via SQL to keep this test self-contained. The filter builds a
    NOT EXISTS subquery against context_embedding.
    """
    repo = ContextRepoImpl(migrated_db)
    repo.create(_full_item(id="done", owner_user_id="owner"))
    repo.create(_full_item(id="pending", owner_user_id="owner"))

    # Mark 'done' as embedded for bge-m3 (the seeded default model).
    migrated_db.execute(
        "INSERT INTO context_embedding (item_id, model_slug, embedded_at, status) "
        "VALUES (?, 'bge-m3', ?, 'done')",
        ("done", 1_700_000_000),
    )

    rows, _ = repo.list(ItemFilter(not_done_for_model="bge-m3", limit=10))
    ids = {r.id for r in rows}
    assert "pending" in ids
    assert "done" not in ids


def test_list_clamps_invalid_limit_to_default(migrated_db: sqlite3.Connection) -> None:
    """limit<=0 or >200 falls back to 50 (Go's clamp; asymmetric with Searcher)."""
    repo = ContextRepoImpl(migrated_db)
    # Create more than 50 items to detect the clamp by row count.
    for i in range(55):
        repo.create(
            _full_item(
                id=f"clamp-{i:02d}",
                owner_user_id="u-clamp",
                created_at=1_700_000_000 + i,
                updated_at=1_700_000_000 + i,
            )
        )

    rows, next_cursor = repo.list(ItemFilter(owner_user_id="u-clamp", limit=0))
    assert len(rows) == 50
    assert next_cursor != ""

    # limit > 200 also clamps to 50.
    rows, next_cursor = repo.list(ItemFilter(owner_user_id="u-clamp", limit=999))
    assert len(rows) == 50
    assert next_cursor != ""


# ---------------------------------------------------------------------------
# next_cursor
# ---------------------------------------------------------------------------


def test_next_cursor_encodes_created_at_and_id(migrated_db: sqlite3.Connection) -> None:
    """next_cursor(item) is encode_cursor(item.created_at, item.id)."""
    repo = ContextRepoImpl(migrated_db)
    item = _full_item(id="cur-1", created_at=1_719_398_400)
    assert repo.next_cursor(item) == encode_cursor(1_719_398_400, "cur-1")


# ---------------------------------------------------------------------------
# Cursor format — byte-identical with Go's strconv.FormatInt(ts, 36)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ts", "expected_head"),
    [
        # Authoritative outputs from Go's strconv.FormatInt(ts, 36),
        # cross-checked against the migration spike.
        (0, "0"),
        (35, "z"),
        (36, "10"),
        (1_719_398_400, "sfooao"),
        (1_718_000_000, "seupa8"),
    ],
)
def test_encode_cursor_matches_go_strconv(ts: int, expected_head: str) -> None:
    """encode_cursor produces Go-identical base36 encoding for the head."""
    assert encode_cursor(ts, "abc-123") == f"{expected_head}:abc-123"


def test_decode_cursor_round_trips_with_encode() -> None:
    """decode_cursor(encode_cursor(ts, id)) == (ts, id) for various ts."""
    for ts in (0, 1, 35, 36, 1_700_000_000, 1_719_398_400):
        cursor = encode_cursor(ts, "the-id")
        decoded_ts, decoded_id = decode_cursor(cursor)
        assert decoded_ts == ts
        assert decoded_id == "the-id"


def test_decode_cursor_rejects_malformed_input() -> None:
    """Missing colon → ValueError."""
    with pytest.raises(ValueError, match="malformed cursor"):
        decode_cursor("no-colon-here")


def test_decode_cursor_accepts_negative_ts() -> None:
    """Forward-compat: a leading '-' decodes as a negative ts."""
    ts, item_id = decode_cursor("-5:abc")
    assert ts == -5
    assert item_id == "abc"


# ---------------------------------------------------------------------------
# reindex_fts
# ---------------------------------------------------------------------------


def test_reindex_fts_makes_externalized_item_searchable(
    migrated_db: sqlite3.Connection,
) -> None:
    """reindex_fts rewrites the FTS row so MATCH finds the hydrated content.

    Mirrors the IngestService externalization flow: an item is created
    with empty content (content lives in FileStore); the AFTER INSERT
    trigger captured empty content; reindex_fts writes the hydrated bytes
    into context_fts so FTS MATCH finds it.
    """
    repo = ContextRepoImpl(migrated_db)
    item = _full_item(
        id="ext-1",
        content="",  # externalized — content lives out-of-band
        content_uri="filestore://ext-1.bin",
        title="Rare topic",
        summary="",
        owner_user_id="owner",
    )
    repo.create(item)

    # Sanity: BEFORE reindex, MATCH 'hydrated' returns nothing because
    # the trigger captured the empty content column.
    (n_before,) = migrated_db.execute(
        "SELECT count(*) FROM context_fts WHERE context_fts MATCH 'hydrated'"
    ).fetchone()
    assert n_before == 0

    # Reindex with the hydrated content.
    repo.reindex_fts(item.id, title="Rare topic", summary="", content="hydrated payload")

    # AFTER reindex, MATCH 'hydrated' finds the row.
    (n_after,) = migrated_db.execute(
        "SELECT count(*) FROM context_fts WHERE context_fts MATCH 'hydrated'"
    ).fetchone()
    assert n_after == 1


def test_reindex_fts_is_idempotent_with_same_args(
    migrated_db: sqlite3.Connection,
) -> None:
    """Calling reindex_fts twice with the SAME args leaves one FTS row.

    The real-world retry case is IngestService re-running reindex_fts
    after a downstream rollback — the arguments are the same hydrated
    bytes, so the FTS5 external-content 'delete' command finds the
    matching row and replaces it cleanly.

    Note: reindex_fts is NOT idempotent across DIFFERENT argument sets —
    FTS5's external-content 'delete' command requires the supplied
    values to match the row currently in the index. This mirrors Go's
    behavior (the Go code uses the same SQL shape).
    """
    repo = ContextRepoImpl(migrated_db)
    # Inline content so the AFTER INSERT trigger writes matching values;
    # reindex_fts with the same (title, summary, content) is then a true
    # no-op rewrite.
    item = _full_item(id="ext-idem", content="inline", owner_user_id="owner")
    repo.create(item)

    repo.reindex_fts(item.id, item.title, item.summary, item.content)
    repo.reindex_fts(item.id, item.title, item.summary, item.content)

    # Count FTS rows for this item's rowid — should be exactly 1.
    (rowid,) = migrated_db.execute(
        "SELECT rowid FROM context_item WHERE id = ?", (item.id,)
    ).fetchone()
    (n,) = migrated_db.execute(
        "SELECT count(*) FROM context_fts WHERE rowid = ?", (rowid,)
    ).fetchone()
    assert n == 1


def test_reindex_fts_missing_raises_item_not_found(
    migrated_db: sqlite3.Connection,
) -> None:
    """reindex_fts on an unknown id raises ItemNotFound.

    The delete statement affects zero rows, which the impl detects via
    cur.rowcount == 0.
    """
    repo = ContextRepoImpl(migrated_db)
    with pytest.raises(ItemNotFound) as exc_info:
        repo.reindex_fts("no-such-id", "t", "s", "c")
    assert exc_info.value.item_id == "no-such-id"


# ---------------------------------------------------------------------------
# scan_item NULL handling via the row_factory
# ---------------------------------------------------------------------------


def test_scan_item_coerces_null_owner_to_empty_string(
    migrated_db: sqlite3.Connection,
) -> None:
    """NULL owner_user_id reads back as "" via scan_item.

    The schema marks owner_user_id NULLABLE; Go's sql.NullString zero
    value is "". scan_item mirrors this with `value or ""`. We verify
    by directly inserting a NULL via SQL (bypassing the repo's nullable()
    helper, which would have stored "" anyway) and reading back through
    the row_factory.
    """
    # Insert a GLOBAL-scope row with owner_user_id=NULL directly via SQL.
    # GLOBAL scope forbids owner_user_id (per _validate_combination), so
    # going through new_context_item would reject it — we use raw SQL.
    migrated_db.execute(
        """
        INSERT INTO context_item (
            id, scope, kind, source, owner_user_id, project_id, agent_id,
            conversation_id, parent_id, title, summary, content, content_uri,
            content_mime, content_hash, language, tags, source_meta, visibility,
            confidence, word_count, any_embedding, created_at, updated_at, version
        ) VALUES (
            'null-owner', 'global', 'note', 'manual', NULL, NULL, NULL,
            NULL, NULL, 't', 's', 'c', NULL, NULL, NULL, NULL,
            '[]', '{}', 'private', 1.0, 0, 0, 1700000000, 1700000000, 1
        )
        """
    )

    repo = ContextRepoImpl(migrated_db)
    got = repo.get("null-owner")

    # NULL → "" coercion (mirrors Go's sql.NullString.String zero value).
    assert got.owner_user_id == ""
    assert got.owner_user_id is not None  # the bug would be `None` slipping through
    assert got.project_id == ""
    assert got.agent_id == ""
    assert got.conversation_id == ""
    assert got.parent_id == ""
    assert got.content_uri == ""
    assert got.content_mime == ""
    assert got.content_hash == ""
    assert got.language == ""

    # Numeric fields stay numeric (the `or ""` shortcut would corrupt 0).
    assert got.word_count == 0
    assert got.any_embedding == 0
    assert got.version == 1
    assert got.confidence == 1.0
    assert got.created_at == 1_700_000_000


def test_scan_item_decodes_empty_json_columns_to_defaults(
    migrated_db: sqlite3.Connection,
) -> None:
    """Empty tags/source_meta strings decode to [] / {} (not None)."""
    migrated_db.execute(
        """
        INSERT INTO context_item (
            id, scope, kind, source, owner_user_id, title, tags, source_meta,
            visibility, confidence, word_count, any_embedding,
            created_at, updated_at, version
        ) VALUES (
            'empty-json', 'user', 'note', 'manual', 'u1', 't',
            '[]', '{}', 'private', 1.0, 0, 0, 1700000000, 1700000000, 1
        )
        """
    )

    repo = ContextRepoImpl(migrated_db)
    got = repo.get("empty-json")
    assert got.tags == []
    assert got.source_meta == {}


def test_scan_item_passes_through_non_context_item_selects(
    migrated_db: sqlite3.Connection,
) -> None:
    """The connection-level row_factory only kicks in for context_item SELECTs.

    Other SELECTs (PRAGMA, vec_version, schema_meta, ...) must return raw
    tuples, not ContextItem instances. Otherwise the ping in open_db and
    the migration runner's version probe would both break.
    """
    # schema_meta probe — should be a tuple, not a ContextItem.
    row = migrated_db.execute(
        "SELECT key, value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert isinstance(row, tuple)
    assert row == ("schema_version", "5")

    # PRAGMA probe — same.
    (fk,) = migrated_db.execute("PRAGMA foreign_keys").fetchone()
    assert fk == 1


# ---------------------------------------------------------------------------
# Protocol structural check
# ---------------------------------------------------------------------------


def test_context_repo_impl_satisfies_protocol(migrated_db: sqlite3.Connection) -> None:
    """ContextRepoImpl should be recognized as a ContextRepo by typing.runtime_checkable.

    Guards against accidental signature drift (e.g. a method rename).
    """
    from unictx.items.repo import ContextRepo

    repo = ContextRepoImpl(migrated_db)
    assert isinstance(repo, ContextRepo)
