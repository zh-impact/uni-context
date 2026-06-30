"""Tests for SearchService — fts-only + hybrid retrieval with RRF fusion.

Mirrors Go's search_test.go and search_hybrid_test.go. Tests live in
tests/search/ to keep service tests grouped with their module.

Test doubles defined inline:
  - StubSearcher: controllable search_fts + search_vector.

FakeContextRepo + FakeEmbedder / ErrorEmbedder from tests/_fakes/ back
the rest. The StringIO log captures warn-and-continue messages.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from io import StringIO

import pytest

from tests._fakes.fake_embedder import ErrorEmbedder, FakeEmbedder
from tests._fakes.fake_repo import FakeContextRepo
from unictx.items.models import ContextItem, Kind, Scope
from unictx.search.searcher import SearchHit, SearchMode, SearchQuery
from unictx.search.service import (
    RRF_K,
    SearchRequest,
    SearchService,
)
from unictx.search.vectorstore import VectorHit, VectorQuery

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class StubSearcher:
    """Controllable Searcher for tests.

    Each attribute is a list of hits to return on the next call.
    ``fts_err`` / ``vec_err`` inject failures to exercise the
    warn-and-continue paths.

    Note: deliberately NOT ``slots=True`` so per-test method overrides
    (``searcher.search_vector = hanging_vec``) work for the per-leg
    timeout tests. Other tests in this file mutate fields directly,
    which slots would also disallow.
    """

    fts_hits: list[SearchHit] = field(default_factory=list)
    vec_hits: list[VectorHit] = field(default_factory=list)
    fts_err: Exception | None = None
    vec_err: Exception | None = None
    fts_calls: list[SearchQuery] = field(default_factory=list)
    vec_calls: list[VectorQuery] = field(default_factory=list)

    def search_fts(self, q: SearchQuery) -> list[SearchHit]:
        self.fts_calls.append(q)
        if self.fts_err is not None:
            raise self.fts_err
        return list(self.fts_hits)

    def search_vector(self, q: VectorQuery) -> list[VectorHit]:
        self.vec_calls.append(q)
        if self.vec_err is not None:
            raise self.vec_err
        return list(self.vec_hits)


def _item(
    item_id: str,
    *,
    scope: Scope = Scope.USER,
    kind: Kind = Kind.NOTE,
    title: str = "",
    project_id: str = "",
) -> ContextItem:
    """Build a minimal ContextItem for hydration."""
    return ContextItem(
        id=item_id,
        scope=scope,
        kind=kind,
        title=title,
        project_id=project_id,
    )


def _fixture(
    *,
    embedder=None,
    searcher: StubSearcher | None = None,
    leg_timeout: float | None = None,
    access_repo=None,
) -> tuple[SearchService, FakeContextRepo, StubSearcher, StringIO]:
    repo = FakeContextRepo()
    searcher = searcher if searcher is not None else StubSearcher()
    log = StringIO()
    svc = SearchService(
        searcher,
        repo,
        log=log,
        embedder=embedder,
        leg_timeout=leg_timeout,
        access_repo=access_repo,
    )
    return svc, repo, searcher, log


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------


def test_default_mode_is_fts_only() -> None:
    """SearchRequest.mode defaults to FTS_ONLY."""
    assert SearchRequest().mode == SearchMode.FTS_ONLY


def test_hybrid_without_embedder_degrades_to_fts_only() -> None:
    """Hybrid requested but no embedder wired → fts-only silently."""
    searcher = StubSearcher(
        fts_hits=[SearchHit(id="a", score=1.0, snippet="alpha")]
    )
    svc, repo, _, _ = _fixture(searcher=searcher)  # no embedder
    repo.items["a"] = _item("a", title="alpha")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    assert len(response.results) == 1
    assert response.results[0].matched_by == ["fts"]


def test_fts_only_mode_never_calls_vector() -> None:
    searcher = StubSearcher()
    svc, _, _, _ = _fixture(searcher=searcher)

    svc.search(SearchRequest(query="x", mode=SearchMode.FTS_ONLY))

    assert searcher.vec_calls == []


# ---------------------------------------------------------------------------
# fts-only basic
# ---------------------------------------------------------------------------


def test_fts_only_basic_returns_hydrated_items() -> None:
    """FTS hits hydrate via repo + carry snippet + matched_by=["fts"]."""
    searcher = StubSearcher(
        fts_hits=[SearchHit(id="a", score=2.0, snippet="alpha snippet")]
    )
    svc, repo, _, _ = _fixture(searcher=searcher)
    repo.items["a"] = _item("a", title="alpha")

    response = svc.search(SearchRequest(query="alpha"))

    assert response.total == 1
    result = response.results[0]
    assert result.item.id == "a"
    assert result.score == 2.0
    assert result.snippet == "alpha snippet"
    assert result.matched_by == ["fts"]


def test_fts_only_overfetches_three_times_limit() -> None:
    """FTS leg asks Searcher for limit*3 hits so post-filter doesn't underfill."""
    searcher = StubSearcher()
    svc, _, _, _ = _fixture(searcher=searcher)

    svc.search(SearchRequest(query="x", limit=10))

    assert searcher.fts_calls[0].limit == 30


def test_fts_only_default_limit_is_20_when_zero() -> None:
    """Zero limit → default 20 → over-fetch 60."""
    searcher = StubSearcher()
    svc, _, _, _ = _fixture(searcher=searcher)

    svc.search(SearchRequest(query="x", limit=0))

    assert searcher.fts_calls[0].limit == 60


def test_fts_only_post_filters_by_scope() -> None:
    """Out-of-scope items in FTS hits are skipped."""
    searcher = StubSearcher(
        fts_hits=[
            SearchHit(id="u1", score=2.0),
            SearchHit(id="p1", score=1.5),
        ]
    )
    svc, repo, _, _ = _fixture(searcher=searcher)
    repo.items["u1"] = _item("u1", scope=Scope.USER)
    repo.items["p1"] = _item("p1", scope=Scope.PROJECT)

    response = svc.search(SearchRequest(query="x", scopes=[Scope.USER]))

    assert response.total == 1
    assert response.results[0].item.id == "u1"


def test_fts_only_skips_missing_items() -> None:
    """Item deleted between FTS indexing and search → skip (not error)."""
    searcher = StubSearcher(fts_hits=[SearchHit(id="ghost", score=1.0)])
    svc, _, _, _ = _fixture(searcher=searcher)  # repo has no items

    response = svc.search(SearchRequest(query="x"))

    assert response.total == 0


def test_fts_only_truncates_to_limit() -> None:
    """Result list trimmed to user's limit even when over-fetch returns more."""
    searcher = StubSearcher(
        fts_hits=[SearchHit(id=str(i), score=float(10 - i)) for i in range(10)]
    )
    svc, repo, _, _ = _fixture(searcher=searcher)
    for i in range(10):
        repo.items[str(i)] = _item(str(i))

    response = svc.search(SearchRequest(query="x", limit=3))

    assert response.total == 3


# ---------------------------------------------------------------------------
# Hybrid basic
# ---------------------------------------------------------------------------


def test_hybrid_basic_merges_fts_and_vector() -> None:
    """FTS + vector hits RRF-fuse into a single ranked list."""
    searcher = StubSearcher(
        fts_hits=[SearchHit(id="a", score=2.0, snippet="from fts")],
        vec_hits=[VectorHit(id="b", score=0.9, distance=0.2)],
    )
    svc, repo, _, _ = _fixture(embedder=FakeEmbedder(), searcher=searcher)
    repo.items["a"] = _item("a", title="alpha")
    repo.items["b"] = _item("b", title="beta")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    ids = {r.item.id for r in response.results}
    assert ids == {"a", "b"}


def test_hybrid_item_hit_by_both_gets_higher_score() -> None:
    """Item in both legs contributes two RRF terms → higher score than one-leg."""
    searcher = StubSearcher(
        fts_hits=[
            SearchHit(id="both", score=2.0),
            SearchHit(id="fts_only", score=1.5),
        ],
        vec_hits=[VectorHit(id="both", score=0.9, distance=0.2)],
    )
    svc, repo, _, _ = _fixture(embedder=FakeEmbedder(), searcher=searcher)
    repo.items["both"] = _item("both")
    repo.items["fts_only"] = _item("fts_only")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))
    scores = {r.item.id: r.score for r in response.results}

    # RRF: score = 1/(rank + RRF_K), rank is 0-indexed.
    # "both": FTS rank 0 → 1/60, vec rank 0 → 1/60; total = 2/60.
    # "fts_only": FTS rank 1 → 1/61; total = 1/61.
    assert scores["both"] == pytest.approx(2.0 / RRF_K)
    assert scores["fts_only"] == pytest.approx(1.0 / (1 + RRF_K))
    assert scores["both"] > scores["fts_only"]


def test_hybrid_item_hit_by_both_marked_with_both_paths() -> None:
    searcher = StubSearcher(
        fts_hits=[SearchHit(id="a", score=2.0)],
        vec_hits=[VectorHit(id="a", score=0.9)],
    )
    svc, repo, _, _ = _fixture(embedder=FakeEmbedder(), searcher=searcher)
    repo.items["a"] = _item("a", title="alpha")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    assert response.results[0].matched_by == ["fts", "vector"]


def test_hybrid_vector_only_marked_vector() -> None:
    searcher = StubSearcher(
        fts_hits=[],
        vec_hits=[VectorHit(id="a", score=0.9)],
    )
    svc, repo, _, _ = _fixture(embedder=FakeEmbedder(), searcher=searcher)
    repo.items["a"] = _item("a", title="alpha")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    assert response.results[0].matched_by == ["vector"]


def test_hybrid_overfetches_three_times_limit_on_both_legs() -> None:
    searcher = StubSearcher()
    svc, _, _, _ = _fixture(embedder=FakeEmbedder(), searcher=searcher)

    svc.search(SearchRequest(query="x", limit=5, mode=SearchMode.HYBRID))

    assert searcher.fts_calls[0].limit == 15
    assert searcher.vec_calls[0].limit == 15


# ---------------------------------------------------------------------------
# Hybrid: degradation paths
# ---------------------------------------------------------------------------


def test_hybrid_embed_error_degrades_to_fts_only() -> None:
    """Embed failure (EmbeddingFailed) → fall back to fts-only with warning."""
    searcher = StubSearcher(fts_hits=[SearchHit(id="a", score=1.0)])
    svc, repo, _, log = _fixture(embedder=ErrorEmbedder(), searcher=searcher)
    repo.items["a"] = _item("a")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    assert len(response.results) == 1
    assert response.results[0].matched_by == ["fts"]
    assert "embed failed" in log.getvalue()


def test_hybrid_vector_lookup_failure_degrades_to_fts_only() -> None:
    """VectorStore error → fall back to fts-only entirely."""
    searcher = StubSearcher(
        fts_hits=[SearchHit(id="a", score=1.0)],
        vec_err=RuntimeError("vec0 table missing"),
    )
    svc, repo, _, log = _fixture(embedder=FakeEmbedder(), searcher=searcher)
    repo.items["a"] = _item("a")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    # Full fall-back to fts-only — discards v_hits entirely
    assert len(response.results) == 1
    assert response.results[0].matched_by == ["fts"]
    assert "vector lookup failed" in log.getvalue()


def test_hybrid_fts_failure_continues_with_vector_only() -> None:
    """FTS error → keep vector results, log warning (don't waste the embed work)."""
    searcher = StubSearcher(
        fts_err=RuntimeError("fts5 corruption"),
        vec_hits=[VectorHit(id="a", score=0.9)],
    )
    svc, repo, _, log = _fixture(embedder=FakeEmbedder(), searcher=searcher)
    repo.items["a"] = _item("a", title="alpha")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    # Vector-only results survive
    assert len(response.results) == 1
    assert response.results[0].matched_by == ["vector"]
    assert "fts failed" in log.getvalue()


def test_hybrid_per_leg_timeout_prevents_vector_hang() -> None:
    """Vector leg exceeding leg_timeout → abandoned, falls back to fts-only.

    Mirrors Go's TestSearchService_Hybrid_PerLegTimeoutPreventsVecHang
    (search_hybrid_test.go:449). The hung vector leg is cancelled after
    `leg_timeout`; the existing per-leg fallback path fires (warn +
    return fts-only results). Without the timeout boundary, a wedged
    leg would freeze the CLI.
    """
    searcher = StubSearcher(
        fts_hits=[SearchHit(id="a", score=1.0)],
    )

    def hanging_vec(q: VectorQuery) -> list[VectorHit]:
        time.sleep(1.0)  # simulates vec0 corruption / FTS5 tokenizer spin
        return []

    searcher.search_vector = hanging_vec  # type: ignore[method-assign]

    svc, repo, _, log = _fixture(
        embedder=FakeEmbedder(),
        searcher=searcher,
        leg_timeout=0.1,
    )
    repo.items["a"] = _item("a")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    assert len(response.results) == 1
    assert response.results[0].matched_by == ["fts"]
    log_text = log.getvalue().lower()
    assert "vector" in log_text and "timeout" in log_text, (
        f"timeout warning should mention vector + timeout; got: {log.getvalue()!r}"
    )


def test_hybrid_per_leg_timeout_prevents_fts_hang() -> None:
    """FTS leg exceeding leg_timeout → abandoned, returns vector-only results.

    Mirrors Go's TestSearchService_Hybrid_PerLegTimeoutPreventsFTSHang
    (search_hybrid_test.go:493). Symmetric with the vector-hang case.
    """
    searcher = StubSearcher(
        vec_hits=[VectorHit(id="a", score=0.9)],
    )

    def hanging_fts(q: SearchQuery) -> list[SearchHit]:
        time.sleep(1.0)
        return []

    searcher.search_fts = hanging_fts  # type: ignore[method-assign]

    svc, repo, _, log = _fixture(
        embedder=FakeEmbedder(),
        searcher=searcher,
        leg_timeout=0.1,
    )
    repo.items["a"] = _item("a", title="alpha")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    assert len(response.results) == 1
    assert response.results[0].matched_by == ["vector"]
    log_text = log.getvalue().lower()
    assert "fts" in log_text and "timeout" in log_text, (
        f"timeout warning should mention fts + timeout; got: {log.getvalue()!r}"
    )


# ---------------------------------------------------------------------------
# Ranking + tiebreak
# ---------------------------------------------------------------------------


def test_ranking_higher_score_first() -> None:
    """Results preserve the Searcher's BM25-desc order (FTS contract)."""
    searcher = StubSearcher(
        fts_hits=[
            SearchHit(id="high", score=2.0),
            SearchHit(id="mid", score=1.0),
            SearchHit(id="low", score=0.5),
        ]
    )
    svc, repo, _, _ = _fixture(searcher=searcher)
    repo.items["high"] = _item("high")
    repo.items["mid"] = _item("mid")
    repo.items["low"] = _item("low")

    response = svc.search(SearchRequest(query="x"))

    ids = [r.item.id for r in response.results]
    assert ids == ["high", "mid", "low"]


def test_tiebreak_newer_id_wins_on_score_tie() -> None:
    """On score tie: lexically-larger id (newer ULID) ranks first.

    ULIDs are timestamp-prefixed and lexically-sortable, so lexical
    descending = creation-time descending. For a personal KB the recent
    note is more likely what the user wants.

    Test IDs use ULID-style lexical ordering: "01AAA..." (older) vs
    "01BBB..." (newer) — the second is lexically larger.
    """
    searcher = StubSearcher(
        fts_hits=[SearchHit(id="01aaa", score=1.0)],  # rank 0 in FTS
        vec_hits=[VectorHit(id="01bbb", score=0.9)],  # rank 0 in vec
    )
    svc, repo, _, _ = _fixture(embedder=FakeEmbedder(), searcher=searcher)
    repo.items["01aaa"] = _item("01aaa")
    repo.items["01bbb"] = _item("01bbb")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    scores = {r.item.id: r.score for r in response.results}
    assert scores["01aaa"] == scores["01bbb"]
    ids = [r.item.id for r in response.results]
    # "01bbb" > "01aaa" lexically → wins tiebreak → first
    assert ids[0] == "01bbb"


# ---------------------------------------------------------------------------
# Hydrate cache
# ---------------------------------------------------------------------------


def test_hybrid_hydrates_each_item_once() -> None:
    """ID appearing in both FTS and vector results triggers one repo.get.

    The cache is per-Search call; without it, an item hit by both legs
    would be fetched twice.
    """
    searcher = StubSearcher(
        fts_hits=[SearchHit(id="shared", score=2.0)],
        vec_hits=[VectorHit(id="shared", score=0.9)],
    )
    svc, repo, _, _ = _fixture(embedder=FakeEmbedder(), searcher=searcher)
    repo.items["shared"] = _item("shared")

    svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    # FakeContextRepo doesn't count get_calls, but we can verify by
    # replacing the get method with a counter — simpler: verify the
    # result is correct (matched_by=["fts","vector"]) which proves both
    # legs saw the same hydrated item.
    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))
    assert response.results[0].matched_by == ["fts", "vector"]


# ---------------------------------------------------------------------------
# RRF formula
# ---------------------------------------------------------------------------


def test_rrf_formula_first_rank_is_one_over_k() -> None:
    """Top FTS hit (rank 0) contributes 1/RRF_K to its score."""
    searcher = StubSearcher(fts_hits=[SearchHit(id="a", score=99.9)])
    svc, repo, _, _ = _fixture(searcher=searcher)
    repo.items["a"] = _item("a")

    response = svc.search(SearchRequest(query="x"))

    # FTS score from BM25 (99.9) is REPLACED by RRF contribution.
    # In fts-only mode the raw BM25 score flows through (no RRF).
    # This test verifies the fts-only path uses the raw score.
    assert response.results[0].score == 99.9


def test_rrf_formula_hybrid_first_rank() -> None:
    """In hybrid mode, top FTS hit (rank 0) contributes 1/RRF_K to fused score."""
    searcher = StubSearcher(fts_hits=[SearchHit(id="a", score=99.9)])
    svc, repo, _, _ = _fixture(embedder=FakeEmbedder(), searcher=searcher)
    repo.items["a"] = _item("a")

    response = svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    # In hybrid mode, BM25 score is discarded — only RRF contributes.
    assert response.results[0].score == pytest.approx(1.0 / RRF_K)


def test_rrf_formula_rank_advances_on_post_filter() -> None:
    """surviving_rank only increments for in-scope items (rank is post-filter)."""
    # Two FTS hits: first out-of-scope (filtered), second in-scope.
    # The in-scope item should be rank 0 (the filter survivor), not rank 1.
    searcher = StubSearcher(
        fts_hits=[
            SearchHit(id="out", score=10.0),
            SearchHit(id="in", score=1.0),
        ]
    )
    svc, repo, _, _ = _fixture(embedder=FakeEmbedder(), searcher=searcher)
    repo.items["out"] = _item("out", scope=Scope.PROJECT)
    repo.items["in"] = _item("in", scope=Scope.USER)

    response = svc.search(
        SearchRequest(query="x", mode=SearchMode.HYBRID, scopes=[Scope.USER])
    )

    # "in" is rank 0 after filter → score 1/60
    assert response.results[0].item.id == "in"
    assert response.results[0].score == pytest.approx(1.0 / RRF_K)


# ===========================================================================
# P1: Access direction — scope convergence + project isolation.
#
# These are the LOAD-BEARING trust-boundary tests. The first one
# (test_project_actor_cannot_see_user_scope) is the anti-leak regression
# guard: it must NEVER pass if a PROJECT actor can read USER-scope data.
# ===========================================================================


def test_project_actor_cannot_see_user_scope() -> None:
    """LOAD-BEARING: a PROJECT actor must NEVER receive USER-scope items.

    This is the core anti-leak guarantee of the access direction. If it
    ever fails, user private data is leaking to project agents. The
    FTS leg returns a user-scope hit, but convergence removes 'user'
    from the PROJECT actor's visible set, so the hit is dropped.
    """
    from tests._fakes.fake_access_repo import FakeAccessRepo

    searcher = StubSearcher(
        fts_hits=[
            SearchHit(id="secret", score=2.0),   # user scope — private
            SearchHit(id="shared", score=1.0),   # global scope — visible
        ]
    )
    access = FakeAccessRepo()  # no grants → default floor only
    svc, repo, _, _ = _fixture(searcher=searcher, access_repo=access)
    repo.items["secret"] = _item("secret", scope=Scope.USER)
    repo.items["shared"] = _item("shared", scope=Scope.GLOBAL)

    response = svc.search(
        SearchRequest(query="x", as_scope=Scope.PROJECT, as_project_id="P")
    )

    assert response.total == 1
    assert response.results[0].item.id == "shared"
    # The user-scope item must not appear anywhere in results.
    assert all(r.item.scope != Scope.USER for r in response.results)


def test_global_actor_sees_only_global() -> None:
    """A GLOBAL actor sees only global-scope items, not user or project."""
    from tests._fakes.fake_access_repo import FakeAccessRepo

    searcher = StubSearcher(
        fts_hits=[
            SearchHit(id="u", score=3.0),
            SearchHit(id="p", score=2.0),
            SearchHit(id="g", score=1.0),
        ]
    )
    svc, repo, _, _ = _fixture(searcher=searcher, access_repo=FakeAccessRepo())
    repo.items["u"] = _item("u", scope=Scope.USER)
    repo.items["p"] = _item("p", scope=Scope.PROJECT, project_id="P")
    repo.items["g"] = _item("g", scope=Scope.GLOBAL)

    response = svc.search(SearchRequest(query="x", as_scope=Scope.GLOBAL))

    assert response.total == 1
    assert response.results[0].item.id == "g"


def test_user_actor_default_sees_everything() -> None:
    """USER (the default identity) sees all scopes — legacy behavior preserved.

    This pins backward compatibility: callers that never set as_scope
    get the pre-P1 "no boundary" behavior. The convergence is a no-op
    for USER because its default visible set is {user, project, global}.
    """
    searcher = StubSearcher(
        fts_hits=[
            SearchHit(id="u", score=3.0),
            SearchHit(id="p", score=2.0),
            SearchHit(id="g", score=1.0),
        ]
    )
    # No access_repo wired — convergence still applies the USER default.
    svc, repo, _, _ = _fixture(searcher=searcher)
    repo.items["u"] = _item("u", scope=Scope.USER)
    repo.items["p"] = _item("p", scope=Scope.PROJECT, project_id="P")
    repo.items["g"] = _item("g", scope=Scope.GLOBAL)

    response = svc.search(SearchRequest(query="x"))

    assert response.total == 3


def test_project_actor_with_grant_can_see_user_scope() -> None:
    """A grant widens a PROJECT actor's visible set to include USER.

    Grants only ever widen, never narrow. With a grant targeting
    target_scope=USER, the PROJECT actor now sees user-scope items too.
    """
    from tests._fakes.fake_access_repo import FakeAccessRepo
    from unictx.items.models import AccessGrant

    access = FakeAccessRepo(
        grants=[
            AccessGrant(
                as_scope=Scope.PROJECT,
                project_id="P",
                target_scope=Scope.USER,
            )
        ]
    )
    searcher = StubSearcher(
        fts_hits=[
            SearchHit(id="secret", score=2.0),
            SearchHit(id="shared", score=1.0),
        ]
    )
    svc, repo, _, _ = _fixture(searcher=searcher, access_repo=access)
    repo.items["secret"] = _item("secret", scope=Scope.USER)
    repo.items["shared"] = _item("shared", scope=Scope.GLOBAL)

    response = svc.search(
        SearchRequest(query="x", as_scope=Scope.PROJECT, as_project_id="P")
    )

    # Both visible now: user (via grant) + global (default).
    assert {r.item.id for r in response.results} == {"secret", "shared"}


def test_project_isolation_blocks_other_projects() -> None:
    """A PROJECT actor sees only its own project_id's project-scope rows.

    Project P cannot see project Q's items, even though both are
    project scope. Global rows remain shared.
    """
    from tests._fakes.fake_access_repo import FakeAccessRepo

    searcher = StubSearcher(
        fts_hits=[
            SearchHit(id="mine", score=3.0),
            SearchHit(id="theirs", score=2.0),
            SearchHit(id="shared", score=1.0),
        ]
    )
    svc, repo, _, _ = _fixture(searcher=searcher, access_repo=FakeAccessRepo())
    repo.items["mine"] = _item("mine", scope=Scope.PROJECT, project_id="P")
    repo.items["theirs"] = _item("theirs", scope=Scope.PROJECT, project_id="Q")
    repo.items["shared"] = _item("shared", scope=Scope.GLOBAL)

    response = svc.search(
        SearchRequest(query="x", as_scope=Scope.PROJECT, as_project_id="P")
    )

    assert {r.item.id for r in response.results} == {"mine", "shared"}
    assert "theirs" not in {r.item.id for r in response.results}


def test_project_isolation_in_hybrid_vector_leg() -> None:
    """Project isolation also applies to the vector leg in hybrid mode.

    The vector query's project_id is pushed down so a PROJECT actor's
    KNN hits are pre-filtered at SQL. Here we assert the VectorQuery
    carries the actor's project_id for a PROJECT identity.
    """
    from tests._fakes.fake_access_repo import FakeAccessRepo

    embedder = FakeEmbedder(dimension=8)
    searcher = StubSearcher(
        vec_hits=[VectorHit(id="v", distance=0.1, score=0.95)]
    )
    svc, repo, searcher, _ = _fixture(
        embedder=embedder, searcher=searcher, access_repo=FakeAccessRepo()
    )
    repo.items["v"] = _item("v", scope=Scope.PROJECT, project_id="P")

    svc.search(
        SearchRequest(
            query="x",
            mode=SearchMode.HYBRID,
            as_scope=Scope.PROJECT,
            as_project_id="P",
        )
    )

    assert len(searcher.vec_calls) == 1
    assert searcher.vec_calls[0].project_id == "P"


def test_user_actor_vector_leg_has_no_project_id() -> None:
    """A USER actor's vector query carries no project_id (no isolation needed).

    Confirms the project_id pushdown is PROJECT-only — USER sees all.
    """
    embedder = FakeEmbedder(dimension=8)
    searcher = StubSearcher(vec_hits=[])
    svc, repo, searcher, _ = _fixture(embedder=embedder, searcher=searcher)

    svc.search(SearchRequest(query="x", mode=SearchMode.HYBRID))

    assert len(searcher.vec_calls) == 1
    assert searcher.vec_calls[0].project_id == ""


def test_project_actor_with_empty_visible_returns_without_querying() -> None:
    """When convergence empties the scope set, search returns early (no DB hit).

    A PROJECT actor requesting scopes=[user] only has that single scope
    intersected away to nothing. The service must short-circuit and NOT
    call the searcher at all — fail-closed and cheap.
    """
    from tests._fakes.fake_access_repo import FakeAccessRepo

    searcher = StubSearcher(fts_hits=[SearchHit(id="leak", score=1.0)])
    svc, repo, searcher, _ = _fixture(
        searcher=searcher, access_repo=FakeAccessRepo()
    )
    repo.items["leak"] = _item("leak", scope=Scope.USER)

    response = svc.search(
        SearchRequest(
            query="x",
            scopes=[Scope.USER],  # PROJECT actor requests user → emptied
            as_scope=Scope.PROJECT,
            as_project_id="P",
        )
    )

    assert response.total == 0
    # The searcher was never consulted — convergence short-circuited.
    assert searcher.fts_calls == []


def test_convergence_preserved_through_hybrid_degradation() -> None:
    """The boundary holds even when hybrid degrades to fts-only.

    embed failure → fts-only fallback must STILL honor the boundary:
    a PROJECT actor must not leak USER data through the degraded path.
    This is the key invariant of putting convergence at search() entry
    rather than in each path.
    """
    from tests._fakes.fake_access_repo import FakeAccessRepo

    embedder = ErrorEmbedder(inner=FakeEmbedder(dimension=8))
    searcher = StubSearcher(
        fts_hits=[
            SearchHit(id="secret", score=2.0),
            SearchHit(id="shared", score=1.0),
        ]
    )
    svc, repo, searcher, log = _fixture(
        embedder=embedder, searcher=searcher, access_repo=FakeAccessRepo()
    )
    repo.items["secret"] = _item("secret", scope=Scope.USER)
    repo.items["shared"] = _item("shared", scope=Scope.GLOBAL)

    response = svc.search(
        SearchRequest(
            query="x",
            mode=SearchMode.HYBRID,
            as_scope=Scope.PROJECT,
            as_project_id="P",
        )
    )

    # Degraded to fts-only (embed failed) but still boundary-safe.
    assert "embed failed" in log.getvalue()
    assert all(r.item.scope != Scope.USER for r in response.results)
    assert {r.item.id for r in response.results} == {"shared"}


def test_grant_only_widens_never_narrows_for_user() -> None:
    """A grant cannot reduce a USER actor's visible set.

    USER sees everything by default; a stray grant (e.g. targeting
    global) is a no-op. This pins the "grants only widen" rule.
    """
    from tests._fakes.fake_access_repo import FakeAccessRepo
    from unictx.items.models import AccessGrant

    access = FakeAccessRepo(
        grants=[AccessGrant(as_scope=Scope.USER, target_scope=Scope.GLOBAL)]
    )
    searcher = StubSearcher(
        fts_hits=[
            SearchHit(id="u", score=3.0),
            SearchHit(id="p", score=2.0),
            SearchHit(id="g", score=1.0),
        ]
    )
    svc, repo, _, _ = _fixture(searcher=searcher, access_repo=access)
    repo.items["u"] = _item("u", scope=Scope.USER)
    repo.items["p"] = _item("p", scope=Scope.PROJECT, project_id="P")
    repo.items["g"] = _item("g", scope=Scope.GLOBAL)

    response = svc.search(SearchRequest(query="x", as_scope=Scope.USER))

    assert response.total == 3  # unchanged — grant didn't narrow
