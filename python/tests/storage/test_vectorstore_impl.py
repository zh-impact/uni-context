"""Tests for unictx.storage.vectorstore_impl.VectorStoreImpl.

Ports the round-trip and edge-case scenarios that Go exercises in
``archive/go/internal/adapter/sqlite/vectorstore_test.go`` plus the
two regression tests (limit clamp, return-at-most-limit). Together
these cover:

* put/search roundtrip (insert a vector, KNN query returns it with a
  positive score)
* put twice on the same key replaces cleanly (no duplicate rows; the
  vec0 UPSERT idiom is DELETE+INSERT inside a transaction)
* delete removes the row (subsequent search returns nothing)
* KNN finds embedded items and ranks them by distance (one-hot + a
  small perturbation; closest item is first)
* dimension mismatch is surfaced (sqlite-vec rejects wrong-dim blobs)
* limit clamp (``clamp_limit`` from searcher_impl; reused, not
  redefined): ``<=0 -> 20``, ``>200 -> 200``, unchanged otherwise
* scope/kind filters pushed down to context_item via JOIN

The fixture ``migrated_db`` (tests/conftest.py) yields a fresh
``:memory:`` connection with all migrations applied. Migration 0002
already creates the default model row (slug=``bge-m3``, dim=1024,
vec_table=``vec_bge_m3_1024``) and the corresponding vec0 virtual
table, so tests use 1024-dim vectors. We mirror Go's ``vec1024``
helper to keep one-hot vectors readable.
"""

from __future__ import annotations

import sqlite3

import pytest

from unictx.items.models import (
    ContextItem,
    Kind,
    NewItemParams,
    Scope,
    Source,
    new_context_item,
)
from unictx.storage.repo_impl import ContextRepoImpl
from unictx.storage.vectorstore_impl import VectorHit, VectorStoreImpl

# ---------------------------------------------------------------------------
# Helpers — mirror Go's vec1024 / newVectorStoreFixture / putItem.
# ---------------------------------------------------------------------------


def _vec1024(*pairs: tuple[int, float]) -> list[float]:
    """1024-dim sparse vector. ``pairs`` sets (index, value); rest is zero.

    Direct port of Go's ``vec1024`` test helper. The default model's vec0
    table is hardcoded ``FLOAT[1024]`` (migration 0002), so test vectors
    must be 1024-dim. One-hot + a small perturbation in the query is the
    original 4-dim design orthogonality, carried over verbatim.
    """
    v = [0.0] * 1024
    for idx, val in pairs:
        v[idx] = val
    return v


def _make_item(title: str, *, scope: Scope = Scope.USER) -> ContextItem:
    """Build an item. Mirrors Go test helper ``putItem``.

    Default scope is USER; tests that exercise scope filtering pass
    ``scope=Scope.GLOBAL``.
    """
    params = NewItemParams(owner_user_id="u") if scope == Scope.USER else NewItemParams()
    return new_context_item(
        scope,
        Kind.NOTE,
        Source.MANUAL,
        params,
        title=title,
    )


@pytest.fixture
def store(migrated_db: sqlite3.Connection) -> VectorStoreImpl:
    """VectorStoreImpl wired to a migrated :memory: DB.

    Uses the ``migrated_db`` fixture (tests/conftest.py), which applies
    every migration including 0002_embeddings.sql — that seeds the
    default model row (slug=``bge-m3``, dim=1024) and creates the
    ``vec_bge_m3_1024`` vec0 virtual table. Task 2.5 owns reading the
    ``vec_table`` name and operating on the named table; the registry
    itself (Task 2.7) owns populating it.
    """
    return VectorStoreImpl(migrated_db)


def _put_item(
    db: sqlite3.Connection,
    repo: ContextRepoImpl,
    vs: VectorStoreImpl,
    title: str,
    vec: list[float],
    *,
    scope: Scope = Scope.USER,
) -> str:
    """Create a ContextItem (fires AFTER INSERT FTS trigger) and put its vector."""
    item = _make_item(title, scope=scope)
    repo.create(item)
    vs.put("bge-m3", item.id, vec)
    return item.id


# ---------------------------------------------------------------------------
# roundtrip: put then search
# ---------------------------------------------------------------------------


class TestPutAndSearch:
    """put/search roundtrip; KNN ranking; score conversion."""

    def test_put_then_search_returns_item_with_positive_score(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        repo = ContextRepoImpl(migrated_db)
        id1 = _put_item(migrated_db, repo, store, "alpha", _vec1024((0, 1.0)))

        hits = store.search(
            vector=_vec1024((0, 1.0)),
            model_slug="bge-m3",
            limit=5,
        )

        assert len(hits) == 1
        assert hits[0].id == id1
        # Cosine distance for identical normalized vectors is ~0,
        # so score = 1 - 0/2 = 1.0 (in [0,1]).
        assert hits[0].distance == pytest.approx(0.0, abs=1e-6)
        assert hits[0].score == pytest.approx(1.0, abs=1e-6)
        # Score must be > 0 (the brief's invariant).
        assert hits[0].score > 0.0

    def test_knn_ranks_closest_first(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        """One-hot items + query with one dominant axis — closest first.

        Direct port of Go's ``TestVectorStore_PutAndSearch_KNN``.
        """
        repo = ContextRepoImpl(migrated_db)
        id1 = _put_item(migrated_db, repo, store, "go deployment", _vec1024((0, 1.0)))
        _put_item(migrated_db, repo, store, "python scraping", _vec1024((1, 1.0)))
        _put_item(migrated_db, repo, store, "rust async", _vec1024((2, 1.0)))

        # Query is e_0 with a small e_1 component; id1 is closest.
        hits = store.search(
            vector=_vec1024((0, 1.0), (1, 0.1)),
            model_slug="bge-m3",
            limit=3,
        )

        assert len(hits) == 3
        assert hits[0].id == id1, "closest to query must be id1"
        # Distance strictly increases down the result list.
        assert hits[0].distance < hits[1].distance


# ---------------------------------------------------------------------------
# put idempotency: vec0 UPSERT is DELETE+INSERT
# ---------------------------------------------------------------------------


class TestPutIdempotency:
    """Two puts on the same key must replace, not duplicate."""

    def test_put_twice_same_key_no_duplicate(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        repo = ContextRepoImpl(migrated_db)
        id1 = _put_item(migrated_db, repo, store, "title", _vec1024((0, 1.0)))

        # Second put on same key — idempotent UPSERT.
        store.put("bge-m3", id1, _vec1024((0, 1.0)))

        hits = store.search(
            vector=_vec1024((0, 1.0)),
            model_slug="bge-m3",
            limit=5,
        )

        assert len(hits) == 1, "idempotent put must not duplicate"

    def test_put_twice_latest_vector_wins(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        """Second put with a different vector must take effect.

        Verifies the DELETE+INSERT idiom actually replaces the embedding
        (rather than no-op-ing on conflict).
        """
        repo = ContextRepoImpl(migrated_db)
        id1 = _put_item(migrated_db, repo, store, "title", _vec1024((0, 1.0)))

        # Replace embedding: e_0 -> e_1.
        store.put("bge-m3", id1, _vec1024((1, 1.0)))

        # Query e_1; id1 should be the closest match now.
        hits = store.search(
            vector=_vec1024((1, 1.0)),
            model_slug="bge-m3",
            limit=5,
        )
        assert len(hits) == 1
        assert hits[0].id == id1


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    """delete removes the embedding; subsequent search returns nothing."""

    def test_delete_then_search_returns_empty(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        repo = ContextRepoImpl(migrated_db)
        id1 = _put_item(migrated_db, repo, store, "title", _vec1024((0, 1.0)))

        store.delete("bge-m3", id1)

        hits = store.search(
            vector=_vec1024((0, 1.0)),
            model_slug="bge-m3",
            limit=5,
        )
        assert hits == []

    def test_delete_missing_is_noop(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        """Delete of a never-put item_id must not raise.

        Mirrors Go's VectorStore interface contract ("No-op if absent").
        """
        # Should not raise.
        store.delete("bge-m3", "does-not-exist")


# ---------------------------------------------------------------------------
# dimension mismatch
# ---------------------------------------------------------------------------


class TestDimensionMismatch:
    """vec0 rejects vectors of the wrong dimension."""

    def test_put_wrong_dim_raises(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        repo = ContextRepoImpl(migrated_db)
        item = _make_item("title")
        repo.create(item)

        # Default model expects 1024-dim; pass 4-dim.
        wrong = [1.0, 0.0, 0.0, 0.0]
        with pytest.raises((sqlite3.Error, ValueError)):
            store.put("bge-m3", item.id, wrong)


# ---------------------------------------------------------------------------
# limit clamp — direct port of the three Go regression tests.
# ---------------------------------------------------------------------------


class TestLimitClamp:
    """clamp_limit integration: ``<=0 -> 20``, ``>200 -> 200``, identity."""

    def test_search_returns_at_most_limit(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        """12 items, limit=4 — result must be 4, not 12.

        Regression guard for the double over-fetch bug: VectorStore.Search
        used to multiply q.Limit by 3 internally. With the fix, scope/kind
        filters are pushed down to SQL via JOIN and VectorStore returns at
        most q.Limit hits.
        """
        repo = ContextRepoImpl(migrated_db)
        for i in range(12):
            item = _make_item("item")
            repo.create(item)
            store.put("bge-m3", item.id, _vec1024((i, 1.0)))

        hits = store.search(
            vector=_vec1024((0, 1.0)),
            model_slug="bge-m3",
            limit=4,
        )
        assert len(hits) == 4

    def test_limit_above_200_clamped_not_reset(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        """Limit=300 must clamp to 200 (not reset to 20).

        30 indexed items; buggy reset-to-20 would return 20; correct
        clamp-to-200 returns all 30. Cleanly distinguishes the two.
        """
        repo = ContextRepoImpl(migrated_db)
        for i in range(30):
            item = _make_item("item")
            repo.create(item)
            store.put("bge-m3", item.id, _vec1024((i, 1.0)))

        hits = store.search(
            vector=_vec1024((0, 1.0)),
            model_slug="bge-m3",
            limit=300,  # service-layer over-fetch value
        )
        assert len(hits) == 30

    def test_limit_zero_defaults_to_20(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        """Limit=0 must default to 20.

        Direct port of Go's ``TestVectorStore_Search_LimitZeroDefaultsTo20``.
        """
        repo = ContextRepoImpl(migrated_db)
        for i in range(30):
            item = _make_item("item")
            repo.create(item)
            store.put("bge-m3", item.id, _vec1024((i, 1.0)))

        hits = store.search(
            vector=_vec1024((0, 1.0)),
            model_slug="bge-m3",
            limit=0,  # unset -> default 20
        )
        assert len(hits) == 20


# ---------------------------------------------------------------------------
# scope/kind filter pushdown
# ---------------------------------------------------------------------------


class TestScopeKindFilters:
    """Filters pushed down to context_item via JOIN."""

    def test_scope_filter_narrows_results(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        """Direct port of Go's ``TestVectorStore_SearchFiltersByScope``.

        Two items, identical vectors, different scopes — the scope filter
        must narrow to one.
        """
        repo = ContextRepoImpl(migrated_db)

        user_item = _make_item("user note", scope=Scope.USER)
        repo.create(user_item)
        global_item = _make_item("global note", scope=Scope.GLOBAL)
        repo.create(global_item)

        vec = _vec1024((0, 1.0))
        store.put("bge-m3", user_item.id, vec)
        store.put("bge-m3", global_item.id, vec)

        hits = store.search(
            vector=vec,
            model_slug="bge-m3",
            limit=10,
            scopes=["user"],
        )
        assert len(hits) == 1
        assert hits[0].id == user_item.id

    def test_kind_filter_narrows_results(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        """Two items, same vector, different kinds — kind filter narrows."""
        repo = ContextRepoImpl(migrated_db)

        params = NewItemParams(owner_user_id="u")
        note_item = new_context_item(Scope.USER, Kind.NOTE, Source.MANUAL, params, title="note")
        doc_item = new_context_item(Scope.USER, Kind.DOC, Source.MANUAL, params, title="doc")
        repo.create(note_item)
        repo.create(doc_item)

        vec = _vec1024((0, 1.0))
        store.put("bge-m3", note_item.id, vec)
        store.put("bge-m3", doc_item.id, vec)

        hits = store.search(
            vector=vec,
            model_slug="bge-m3",
            limit=10,
            kinds=[str(Kind.DOC)],
        )
        assert len(hits) == 1
        assert hits[0].id == doc_item.id


# ---------------------------------------------------------------------------
# real (dense) dimension — port of Go's TestVectorStore_RealDimension.
# ---------------------------------------------------------------------------


class TestRealDimension:
    """1024-dim dense vector (non-sparse) round-trip end-to-end."""

    def test_dense_vector_put_and_search(
        self,
        migrated_db: sqlite3.Connection,
        store: VectorStoreImpl,
    ) -> None:
        """Direct port of Go's ``TestVectorStore_RealDimension``.

        Exercises the real bge-m3 dimension (1024) with a dense vector —
        guards against a sparse-only test regression.
        """
        repo = ContextRepoImpl(migrated_db)
        item = _make_item("title")
        repo.create(item)

        vec = [float(i % 10) for i in range(1024)]
        store.put("bge-m3", item.id, vec)

        hits = store.search(
            vector=vec,
            model_slug="bge-m3",
            limit=1,
        )
        assert len(hits) == 1
        assert hits[0].id == item.id
        assert hits[0].score > 0.0


# ---------------------------------------------------------------------------
# ModelNotFound — port of Go's lookup-error path.
# ---------------------------------------------------------------------------


class TestModelNotFound:
    """Unknown model slug raises ModelNotFound (wrapping sql.ErrNoRows)."""

    def test_put_unknown_model_raises(
        self,
        store: VectorStoreImpl,
    ) -> None:
        from unictx.storage.vectorstore_impl import ModelNotFound

        with pytest.raises(ModelNotFound):
            store.put("no-such-model", "id", [1.0])

    def test_search_unknown_model_raises(
        self,
        store: VectorStoreImpl,
    ) -> None:
        from unictx.storage.vectorstore_impl import ModelNotFound

        with pytest.raises(ModelNotFound):
            store.search(
                vector=[0.0] * 1024,
                model_slug="no-such-model",
                limit=5,
            )

    def test_delete_unknown_model_raises(
        self,
        store: VectorStoreImpl,
    ) -> None:
        from unictx.storage.vectorstore_impl import ModelNotFound

        with pytest.raises(ModelNotFound):
            store.delete("no-such-model", "id")


# ---------------------------------------------------------------------------
# VectorHit dataclass — shape parity check (matches search/vectorstore.py).
# ---------------------------------------------------------------------------


class TestVectorHit:
    """VectorHit exported from vectorstore_impl re-exports the Protocol type."""

    def test_vector_hit_has_expected_fields(self) -> None:
        h = VectorHit(id="x", score=0.5, distance=1.0)
        assert h.id == "x"
        assert h.score == 0.5
        assert h.distance == 1.0
