"""Tests for unictx.storage.embedding_repo_impl.EmbeddingRepoImpl.

Ports Go's ``archive/go/internal/adapter/sqlite/embedding_repo_test.go``
scenarios (reconstructed from the brief — the Go test file isn't in the
archive). Covers:

* ``upsert_status`` fresh INSERT (attempts=1, embedded_at populated)
* ``upsert_status`` UPSERT on same key increments attempts AND overwrites
  status/error/last_error
* ``get_status`` happy path returns all 7 fields correctly
* ``get_status`` missing row raises :class:`StatusNotFound` with the
  right attributes
* ``list_failed`` filters status='failed', orders embedded_at ASC,
  caps at the requested limit, returns [] when no failures, defaults
  to 100 when limit <= 0
* ``list_for_item`` returns all model rows for an item, ordered
  model_slug ASC, returns [] when no rows
* Cascade: deleting a ``context_item`` removes its ``context_embedding``
  rows via FK ON DELETE CASCADE (migration 0002 + 0004)

The fixture ``migrated_db`` (tests/conftest.py) yields a fresh
``:memory:`` connection with all migrations applied. Migration 0002
seeds the default ``bge-m3`` model row (FK target for model_slug), so
tests can reference ``bge-m3`` without manual seeding. For tests that
need additional model slugs, we insert them via raw SQL (the
model_registry is Task 2.7, not yet ported).

The brief typo (``ORDER BY embedded_at DESC`` for ``list_failed``) is
documented in the impl module docstring; we follow Go + Protocol (ASC).
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from unictx.embed.embedding_repo import EmbeddingRepo, EmbeddingStatus
from unictx.embed.errors import StatusNotFound
from unictx.storage.embedding_repo_impl import EmbeddingRepoImpl

_INSERT_ITEM_SQL = """
INSERT INTO context_item (
    id, scope, kind, source, owner_user_id, title, tags, source_meta,
    visibility, confidence, word_count, any_embedding,
    created_at, updated_at, version
) VALUES (
    ?, 'user', 'note', 'manual', 'u1', 't', '[]', '{}',
    'private', 1.0, 0, 0, 1700000000, 1700000000, 1
)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_item(db: sqlite3.Connection, item_id: str) -> None:
    """Insert a minimal context_item row.

    We bypass :class:`ContextRepoImpl` because (a) the cascade test
    needs direct DELETE access anyway, and (b) it keeps the test surface
    narrow — failures here point at EmbeddingRepoImpl, not the repo
    layer.
    """
    db.execute(_INSERT_ITEM_SQL, (item_id,))


def _insert_model(db: sqlite3.Connection, slug: str) -> None:
    """Insert a minimal embedding_model row.

    The default 'bge-m3' row is seeded by migration 0002; tests that
    need additional slugs use this. The full model_registry impl is
    Task 2.7 — for now we just need the FK target row.
    """
    db.execute(
        """
        INSERT INTO embedding_model (
            slug, name, provider, dimension, vec_table,
            is_default, status, config, created_at
        ) VALUES (?, ?, 'ollama', 8, ?, 0, 'active', '{}', 1700000000)
        """,
        (slug, slug, f"vec_{slug}_8"),
    )


def _insert_status_row(
    db: sqlite3.Connection,
    *,
    item_id: str,
    model_slug: str,
    status: str,
    embedded_at: int,
    error: str | None = None,
    last_error: str | None = None,
    attempts: int = 1,
) -> None:
    """Insert a status row directly via SQL.

    Used by ``list_failed`` / ``list_for_item`` tests that need to seed
    multiple rows with controlled ``embedded_at`` values (going through
    upsert_status would overwrite ``embedded_at`` with the current time).
    """
    db.execute(
        """
        INSERT INTO context_embedding
            (item_id, model_slug, embedded_at, status, error, last_error, attempts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (item_id, model_slug, embedded_at, status, error, last_error, attempts),
    )


# ---------------------------------------------------------------------------
# Protocol satisfaction — structural typing check
# ---------------------------------------------------------------------------


def test_impl_satisfies_protocol(migrated_db: sqlite3.Connection) -> None:
    """EmbeddingRepoImpl must structurally satisfy the EmbeddingRepo Protocol.

    runtime_checkable Protocols only check method existence, not
    signatures — but combined with the type-annotated test below, this
    catches accidental method-name drift.
    """
    repo = EmbeddingRepoImpl(migrated_db)
    assert isinstance(repo, EmbeddingRepo)


# ---------------------------------------------------------------------------
# upsert_status
# ---------------------------------------------------------------------------


def test_upsert_status_creates_row_with_attempts_one(
    migrated_db: sqlite3.Connection,
) -> None:
    """Fresh INSERT creates a row with attempts=1 and populates embedded_at."""
    _insert_item(migrated_db, "i1")
    repo = EmbeddingRepoImpl(migrated_db)

    before = int(time.time())
    repo.upsert_status("i1", "bge-m3", "done", err_str="")
    after = int(time.time())

    got = repo.get_status("i1", "bge-m3")
    assert got.item_id == "i1"
    assert got.model_slug == "bge-m3"
    assert got.status == "done"
    assert got.error == ""
    assert got.last_error == ""
    assert got.attempts == 1
    assert before <= got.embedded_at <= after


def test_upsert_status_increments_attempts_and_overwrites_fields(
    migrated_db: sqlite3.Connection,
) -> None:
    """Second UPSERT on same key: attempts++, embedded_at/status/error overwritten."""
    _insert_item(migrated_db, "i1")
    repo = EmbeddingRepoImpl(migrated_db)

    # First write — failed with an error.
    repo.upsert_status("i1", "bge-m3", "failed", err_str="boom")
    first = repo.get_status("i1", "bge-m3")
    assert first.attempts == 1
    assert first.status == "failed"
    assert first.error == "boom"
    assert first.last_error == "boom"

    # Second write — same key, new status, new error. Capture the
    # pre-write timestamp so we can assert embedded_at advanced without
    # paying the 1.1s sleep tax. Production uses int(datetime.now(UTC)),
    # so any clock advance (even sub-second on a fast machine) produces
    # a strictly-greater embedded_at when the second write lands in a
    # later second than before_first.
    before_second = int(time.time())
    repo.upsert_status("i1", "bge-m3", "done", err_str="")
    second = repo.get_status("i1", "bge-m3")
    assert second.attempts == 2
    assert second.status == "done"
    assert second.error == ""
    assert second.last_error == ""
    # embedded_at is seconds-resolution. If both writes happened within
    # the same second, the second embedded_at equals first; otherwise
    # strictly greater. The contract is "embedded_at reflects the most
    # recent write" — assert it's >= the pre-second-write timestamp,
    # which proves the row was updated.
    assert second.embedded_at >= before_second


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


def test_get_status_happy_path_returns_all_fields(
    migrated_db: sqlite3.Connection,
) -> None:
    """get_status returns an EmbeddingStatus with all 7 fields populated."""
    _insert_item(migrated_db, "i1")
    # Insert with non-empty error text + non-default attempts to verify
    # the read path doesn't coerce / drop them.
    _insert_status_row(
        migrated_db,
        item_id="i1",
        model_slug="bge-m3",
        status="failed",
        embedded_at=1_700_000_000,
        error="orig",
        last_error="recent",
        attempts=3,
    )
    repo = EmbeddingRepoImpl(migrated_db)

    got = repo.get_status("i1", "bge-m3")
    assert got == EmbeddingStatus(
        item_id="i1",
        model_slug="bge-m3",
        status="failed",
        error="orig",
        last_error="recent",
        attempts=3,
        embedded_at=1_700_000_000,
    )


def test_get_status_not_found_raises_status_not_found(
    migrated_db: sqlite3.Connection,
) -> None:
    """Missing row raises StatusNotFound with item_id + model_slug attributes."""
    repo = EmbeddingRepoImpl(migrated_db)

    with pytest.raises(StatusNotFound) as exc_info:
        repo.get_status("missing-item", "bge-m3")

    err = exc_info.value
    assert err.item_id == "missing-item"
    assert err.model_slug == "bge-m3"


def test_get_status_null_error_coalesces_to_empty_string(
    migrated_db: sqlite3.Connection,
) -> None:
    """SQL NULL in error/last_error surfaces as "" (mirrors Go's sql.NullString)."""
    _insert_item(migrated_db, "i1")
    # Insert with NULL error + NULL last_error directly via SQL — upsert
    # always binds a string, so we can only exercise NULL via raw INSERT.
    _insert_status_row(
        migrated_db,
        item_id="i1",
        model_slug="bge-m3",
        status="done",
        embedded_at=1_700_000_000,
        error=None,
        last_error=None,
    )
    repo = EmbeddingRepoImpl(migrated_db)

    got = repo.get_status("i1", "bge-m3")
    assert got.error == ""
    assert got.last_error == ""
    assert got.error is not None  # the bug would be `None` slipping through


# ---------------------------------------------------------------------------
# list_failed
# ---------------------------------------------------------------------------


def test_list_failed_filters_and_orders_oldest_first(
    migrated_db: sqlite3.Connection,
) -> None:
    """Only status='failed' rows, ordered embedded_at ASC (oldest first)."""
    _insert_item(migrated_db, "i1")
    _insert_item(migrated_db, "i2")
    _insert_item(migrated_db, "i3")
    _insert_model(migrated_db, "extra-model")  # FK target for non-default slug
    # Mixed: failed@300, done@100, failed@200, failed@400.
    _insert_status_row(
        migrated_db, item_id="i1", model_slug="bge-m3", status="failed", embedded_at=300
    )
    _insert_status_row(
        migrated_db, item_id="i2", model_slug="bge-m3", status="done", embedded_at=100
    )
    _insert_status_row(
        migrated_db, item_id="i1", model_slug="extra-model", status="failed", embedded_at=200
    )
    _insert_status_row(
        migrated_db, item_id="i3", model_slug="bge-m3", status="failed", embedded_at=400
    )
    repo = EmbeddingRepoImpl(migrated_db)

    got = repo.list_failed(limit=100)

    # Three failures (i2/done excluded), ordered ASC by embedded_at.
    assert [(r.item_id, r.model_slug, r.embedded_at) for r in got] == [
        ("i1", "extra-model", 200),
        ("i1", "bge-m3", 300),
        ("i3", "bge-m3", 400),
    ]
    assert all(r.status == "failed" for r in got)


def test_list_failed_returns_empty_list_when_no_failures(
    migrated_db: sqlite3.Connection,
) -> None:
    """No 'failed' rows → empty list (NOT None)."""
    _insert_item(migrated_db, "i1")
    _insert_status_row(
        migrated_db, item_id="i1", model_slug="bge-m3", status="done", embedded_at=100
    )
    repo = EmbeddingRepoImpl(migrated_db)

    got = repo.list_failed(limit=100)
    assert got == []
    assert got is not None


def test_list_failed_caps_at_limit(migrated_db: sqlite3.Connection) -> None:
    """limit is honored (we get exactly limit rows even if more exist)."""
    # Seed 3 items, each with one failed row.
    for i in range(3):
        _insert_item(migrated_db, f"i{i}")
        _insert_status_row(
            migrated_db,
            item_id=f"i{i}",
            model_slug="bge-m3",
            status="failed",
            embedded_at=100 + i,
        )
    repo = EmbeddingRepoImpl(migrated_db)

    got = repo.list_failed(limit=2)
    assert len(got) == 2
    # ASC ordering means the oldest two come first.
    assert [r.item_id for r in got] == ["i0", "i1"]


def test_list_failed_limit_zero_or_negative_defaults_to_100(
    migrated_db: sqlite3.Connection,
) -> None:
    """limit <= 0 → 100. Seed 101 failures; verify cap at 100 (Go behavior)."""
    for i in range(101):
        item_id = f"item{i:03d}"
        _insert_item(migrated_db, item_id)
        _insert_status_row(
            migrated_db,
            item_id=item_id,
            model_slug="bge-m3",
            status="failed",
            embedded_at=1_700_000_000 + i,
        )
    repo = EmbeddingRepoImpl(migrated_db)

    # Both 0 and -1 should resolve to the default 100.
    got_zero = repo.list_failed(limit=0)
    got_neg = repo.list_failed(limit=-1)
    assert len(got_zero) == 100
    assert len(got_neg) == 100
    # ASC ordering: the oldest 100 of the 101 seeded failures.
    assert got_zero[0].item_id == "item000"
    assert got_zero[-1].item_id == "item099"


# ---------------------------------------------------------------------------
# list_for_item
# ---------------------------------------------------------------------------


def test_list_for_item_returns_all_models_ordered_by_slug(
    migrated_db: sqlite3.Connection,
) -> None:
    """All model rows for an item, ordered model_slug ASC."""
    _insert_item(migrated_db, "i1")
    _insert_model(migrated_db, "zeta-model")
    _insert_model(migrated_db, "alpha-model")
    # Insert out of order to verify ORDER BY sorts them.
    _insert_status_row(
        migrated_db, item_id="i1", model_slug="zeta-model", status="done", embedded_at=100
    )
    _insert_status_row(
        migrated_db, item_id="i1", model_slug="bge-m3", status="failed", embedded_at=200
    )
    _insert_status_row(
        migrated_db, item_id="i1", model_slug="alpha-model", status="done", embedded_at=300
    )
    repo = EmbeddingRepoImpl(migrated_db)

    got = repo.list_for_item("i1")
    assert [r.model_slug for r in got] == ["alpha-model", "bge-m3", "zeta-model"]
    # Fields are populated (not just slugs).
    assert got[1].status == "failed"
    assert got[1].attempts == 1


def test_list_for_item_returns_empty_list_when_no_rows(
    migrated_db: sqlite3.Connection,
) -> None:
    """No rows for the item → empty list (NOT None)."""
    _insert_item(migrated_db, "i1")  # item exists, but no embeddings
    repo = EmbeddingRepoImpl(migrated_db)

    got = repo.list_for_item("i1")
    assert got == []
    assert got is not None

    # Even for an item that doesn't exist at all — still [], not None.
    got_missing = repo.list_for_item("never-existed")
    assert got_missing == []


# ---------------------------------------------------------------------------
# Cascade (FK ON DELETE CASCADE via migrations 0002 + 0004)
# ---------------------------------------------------------------------------


def test_deleting_context_item_cascades_to_embeddings(
    migrated_db: sqlite3.Connection,
) -> None:
    """Deleting a context_item row removes its context_embedding rows.

    Set up by migrations 0002 (FK with ON DELETE CASCADE on item_id) +
    0004 (rebuild to add ON DELETE CASCADE on model_slug too). We rely
    on ``open_db`` setting ``PRAGMA foreign_keys = ON`` (db.py:125); if
    that PRAGMA isn't set, the cascade silently doesn't fire and this
    test will fail — flagging it loudly.
    """
    _insert_item(migrated_db, "i1")
    repo = EmbeddingRepoImpl(migrated_db)
    repo.upsert_status("i1", "bge-m3", "done", err_str="")

    # Sanity: row exists.
    assert repo.get_status("i1", "bge-m3").item_id == "i1"

    # Delete the parent context_item — cascade should fire.
    migrated_db.execute("DELETE FROM context_item WHERE id = ?", ("i1",))

    # The embedding row must be gone via cascade.
    with pytest.raises(StatusNotFound):
        repo.get_status("i1", "bge-m3")

    # And list_for_item returns [].
    assert repo.list_for_item("i1") == []
