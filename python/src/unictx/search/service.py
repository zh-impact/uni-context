"""SearchService — fts-only + hybrid retrieval with RRF fusion.

Behavior-port of Go's ``internal/service/search.go``. Two modes:

  - **fts-only**: BM25 keyword search via :meth:`Searcher.search_fts`.
    Plan 1 default. Each result carries ``matched_by=["fts"]``.
  - **hybrid**: vector KNN (:meth:`Searcher.search_vector`) AND FTS,
    fused via Reciprocal Rank Fusion (k=60). Items hit by both
    contribute two rank terms. Falls back to fts-only when no embedder
    is wired or the embed call fails.

Hybrid mode execution order (Plan §3.7 clarification):
  1. ``embedder.embed([query])`` — the only HTTP call. The embedder's
     own ``httpx.Client.timeout`` bounds it (60s); no separate
     ``ThreadPoolExecutor`` wrapper. On timeout/error: fall back to
     fts-only with warning.
  2. SQLite queries (FTS via ``search_fts``, KNN via ``search_vector``)
     are local + fast (<100ms typical). **No timeout wrapper needed** —
     they share a connection and run sequentially.
  3. RRF-merge the two result lists.

RRF formula (§3.8): ``score = Σ 1/(rank + 60)`` where rank is
post-filter (an item's rank only increments after it survives the
scope/kind acceptance check).

Over-fetch (§3.9): 3× user limit on both legs so post-filter trimming
doesn't underfill. The Searcher clamps at 200 internally.

Tiebreak: on score tie, the item with the lexically-larger id wins
(newer ULID = newer item, since ULIDs are timestamp-prefixed).

Adaptations vs Go (per Plan §Python Conventions):
  - ctx dropped (Python is sync).
  - Go's ``context.WithTimeout`` per leg reproduced via
    :class:`concurrent.futures.ThreadPoolExecutor` +
    :meth:`Future.result(timeout=...)`. The boundary fires when either
    SQLite leg (FTS or KNN) runs longer than ``leg_timeout`` (default
    5s), preventing a wedged vec0 table or FTS5 tokenizer spin from
    freezing the CLI. The hanging leg is abandoned and the per-leg
    fallback path warns + continues with the other leg.
  - ``log`` defaults to ``sys.stderr``; tests pass ``StringIO``.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field, replace
from typing import IO, TypeVar

from unictx.embed.embedder import Embedder
from unictx.items.models import ContextItem, Kind, Scope, visible_scopes
from unictx.items.repo import AccessRepo, ContextRepo
from unictx.search.searcher import Searcher, SearchHit, SearchMode, SearchQuery
from unictx.search.vectorstore import VectorHit, VectorQuery

_T = TypeVar("_T")

__all__ = [
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "SearchService",
    "RRF_K",
]


# Reciprocal Rank Fusion constant. 60 is the value used in the original
# RRF paper (Cormack et al. 2009); smaller = top ranks dominate more.
# score(d) = Σ 1/(rank_i + RRF_K).
RRF_K = 60

# Default per-leg timeout when leg_timeout is None. Mirrors Go's
# `legTimeoutOrDefault` (search.go) — local SQLite queries usually finish
# in <100ms, but a corrupted vec0 table or FTS5 tokenizer can spin, and
# the boundary prevents a wedged leg from freezing the CLI.
_DEFAULT_LEG_TIMEOUT = 5.0


@dataclass(slots=True)
class SearchRequest:
    """Inputs to a SearchService.search call. Mirrors Go's SearchRequest.

    Access direction (P1):
      as_scope — the access identity of the caller. Determines the
        default visible scope set via :func:`visible_scopes`. Defaults
        to USER (innermost; sees everything), so callers that never set
        it get the legacy "no boundary" behavior.
      as_project_id — when as_scope==PROJECT, the project the caller
        acts as. Enforces project-to-project isolation: a PROJECT actor
        only sees project-scope rows whose project_id matches this.
        Ignored for USER/GLOBAL actors.
    """

    query: str = ""
    scopes: list[Scope] = field(default_factory=list)
    kinds: list[Kind] = field(default_factory=list)
    limit: int = 0
    mode: SearchMode = SearchMode.FTS_ONLY
    as_scope: Scope = Scope.USER
    as_project_id: str = ""


@dataclass(slots=True)
class SearchResult:
    """One row in the search response. Mirrors Go's SearchResult.

    matched_by records which retrieval paths contributed to the score:
    ["fts"], ["vector"], or ["fts","vector"] for items hit by both.
    Order is the order paths were folded in (FTS first), then deduped.
    """

    item: ContextItem
    score: float = 0.0
    snippet: str = ""
    matched_by: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchResponse:
    """Search result envelope. Mirrors Go's SearchResponse."""

    results: list[SearchResult] = field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Filters:
    """Scope/kind acceptance sets + project isolation, built from a SearchRequest.

    None means "all match" — an empty scopes list produces a filter
    that accepts every scope.

    Project isolation (P1): when as_scope==PROJECT, a project-scope item
    is accepted only if its project_id matches as_project_id. Global
    items are never project-scoped, so they pass regardless. USER scope
    items are already excluded by scope convergence before this filter
    runs, so no special handling is needed for them here.
    """

    scopes: dict[Scope, bool] | None = None
    kinds: dict[Kind, bool] | None = None
    as_scope: Scope = Scope.USER
    as_project_id: str = ""

    def accepts(self, item: ContextItem) -> bool:
        # Each guard below is a filter stage; early-return False on the
        # first rejection. SIM103 suggests collapsing to one negated
        # return, but the staged form reads as an explicit filter chain
        # and is easier to extend with future rules.
        if self.scopes is not None and item.scope not in self.scopes:
            return False
        if self.kinds is not None and item.kind not in self.kinds:
            return False
        # Project-to-project isolation: a PROJECT actor only sees its
        # own project's rows. Global rows (scope=global) are shared, so
        # they bypass this check. USER rows can't reach here for a
        # PROJECT actor because scope convergence already removed them.
        is_other_project = (
            self.as_scope == Scope.PROJECT
            and item.scope == Scope.PROJECT
            and item.project_id != self.as_project_id
        )
        return not is_other_project


def _filters_from(request: SearchRequest) -> _Filters:
    """Build a _Filters from a (converged) SearchRequest.

    Pulls the converged scopes/kinds plus the as_scope/as_project_id
    used for project isolation. Callers MUST pass a request that has
    already been through _converge().
    """
    return _Filters(
        scopes=_scope_set(request.scopes),
        kinds=_kind_set(request.kinds),
        as_scope=request.as_scope,
        as_project_id=request.as_project_id,
    )


def _scope_set(scopes: list[Scope]) -> dict[Scope, bool] | None:
    """Empty list → None (all match); else dict for O(1) lookup."""
    if not scopes:
        return None
    return {s: True for s in scopes}


def _kind_set(kinds: list[Kind]) -> dict[Kind, bool] | None:
    if not kinds:
        return None
    return {k: True for k in kinds}


def _dedupe_strings(values: list[str]) -> list[str]:
    """Unique strings preserving first-seen order.

    Used for matched_by: an item hit by both FTS and vector ends up
    with matched_by = ["fts","vector"], not ["fts","vector","fts"].
    """
    if not values:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for s in values:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# SearchService
# ---------------------------------------------------------------------------


class SearchService:
    """FTS + hybrid retrieval. Stateless aside from injected dependencies.

    Construct with ``searcher`` + ``repo`` for fts-only mode. Add
    ``embedder`` for hybrid. ``log`` defaults to ``sys.stderr``;
    tests inject ``StringIO``.

    Access direction (P1): pass ``access_repo`` to enable scope
    convergence at the search() entry. When ``access_repo`` is None,
    convergence uses :func:`visible_scopes` with no grants — the default
    floor still applies (e.g. a PROJECT actor still cannot see USER
    data), but no grant-based widening is possible. This keeps the
    constructor backward-compatible with tests that pre-date P1.
    """

    def __init__(
        self,
        searcher: Searcher,
        repo: ContextRepo,
        log: IO[str] | None = None,
        *,
        embedder: Embedder | None = None,
        leg_timeout: float | None = None,
        access_repo: AccessRepo | None = None,
    ) -> None:
        self._searcher = searcher
        self._repo = repo
        self._log: IO[str] = log if log is not None else sys.stderr
        self._embedder: Embedder | None = embedder
        self._leg_timeout: float = (
            leg_timeout if leg_timeout and leg_timeout > 0 else _DEFAULT_LEG_TIMEOUT
        )
        self._access_repo: AccessRepo | None = access_repo

    def search(self, request: SearchRequest) -> SearchResponse:
        """Dispatch by request.mode. Hybrid without embedder → fts-only.

        Access direction (P1): the FIRST thing this method does is
        converge ``request.scopes`` against the caller's visible scope
        set. Convergence happens here — the single entry point — so the
        fts-only and hybrid paths (and all 4 hybrid degradation paths)
        operate on the same converged scopes. A boundary bug in one
        path cannot leak because the scopes are fixed before dispatch.

        On empty effective scopes, returns an empty SearchResponse
        WITHOUT hitting the DB or embedder — fail-closed and cheap.
        """
        request = self._converge(request)
        if not request.scopes and request.as_scope != Scope.USER:
            # Convergence emptied the scope set for a non-USER actor
            # (e.g. a PROJECT actor whose request.scopes named only
            # 'user'). Nothing visible — return empty without querying.
            # USER always has a non-empty visible set, so this never
            # fires for the default identity.
            return SearchResponse(results=[], total=0)
        if request.mode == SearchMode.HYBRID:
            if self._embedder is None:
                # Hybrid requested but no embedder wired — degrade to
                # fts-only. Plan 1 callers that never set mode are also
                # here (their mode defaults to FTS_ONLY in the dataclass).
                return self._search_fts_only(request)
            return self._search_hybrid(request)
        return self._search_fts_only(request)

    def _converge(self, request: SearchRequest) -> SearchRequest:
        """Apply the access-direction trust boundary to request.scopes.

        Returns a NEW SearchRequest with scopes intersected against the
        visible set for ``request.as_scope`` (widened by grants from
        ``access_repo`` if wired). The original request is not mutated.

        Convergence rule::

            visible    = visible_scopes(as_scope, grants)
            effective  = request.scopes ∩ visible   (if scopes non-empty)
            effective  = visible                     (if scopes empty)

        Project-to-project isolation is enforced downstream by _Filters
        (for the fts/hydrate path) and by VectorQuery (for the vector
        leg), NOT here — it is row-level (depends on each item's
        project_id), not scope-level.
        """
        grants: list = []
        if self._access_repo is not None and request.as_scope != Scope.USER:
            # USER sees everything by default; only non-USER actors can
            # be widened by grants. list_grants returns only the grants
            # matching (as_scope, as_project_id).
            grants = self._access_repo.list_grants(
                request.as_scope, request.as_project_id
            )
        visible = visible_scopes(request.as_scope, grants=grants)
        if not request.scopes:
            effective = visible
        else:
            visible_set = set(visible)
            effective = [s for s in request.scopes if s in visible_set]
        return replace(request, scopes=effective)

    # ---- fts-only path -----------------------------------------------

    def _search_fts_only(self, request: SearchRequest) -> SearchResponse:
        """Plan 1 retrieval: FTS + repo hydrate + scope/kind post-filter.

        Over-fetches 3×limit so post-filter trimming by scope/kind
        doesn't silently underfill the result set. Without this, a
        query whose top-Limit BM25 hits are dominated by out-of-scope
        items would return fewer than Limit results even when more
        in-scope matches exist further down the ranking. Spec §5.2.
        """
        limit = request.limit if request.limit > 0 else 20
        over_fetch = limit * 3

        hits = self._searcher.search_fts(
            SearchQuery(query=request.query, limit=over_fetch)
        )

        filters = _filters_from(request)

        out: list[SearchResult] = []
        for hit in hits:
            if len(out) >= limit:
                break
            item = self._hydrate(hit.id, cache=None)
            if item is None:
                continue
            if not filters.accepts(item):
                continue
            out.append(
                SearchResult(
                    item=item,
                    score=hit.score,
                    snippet=hit.snippet,
                    matched_by=["fts"],
                )
            )

        return SearchResponse(results=out, total=len(out))

    # ---- hybrid path -------------------------------------------------

    def _search_hybrid(self, request: SearchRequest) -> SearchResponse:
        """Vector KNN + FTS, fused via RRF.

        Plan 2a contract: embed errors during hybrid search MUST degrade
        gracefully to fts-only (warn + fall back), never abort the whole
        search. Same applies if the embedder misbehaves (wrong vector
        count) or the vector store fails transiently.
        """
        limit = request.limit if request.limit > 0 else 20
        over_fetch = limit * 3

        # Step 1: embed the query. The only HTTP call in the path —
        # bounded by the embedder's httpx.Client.timeout. On any
        # failure: fall back to fts-only with warning.
        try:
            query_vectors = self._embedder.embed([request.query])  # type: ignore[union-attr]
        except Exception as exc:
            self._warn(
                f"warn: hybrid search embed failed, "
                f"falling back to fts-only: {exc}\n"
            )
            return self._search_fts_only(request)

        if len(query_vectors) != 1:
            self._warn(
                f"warn: hybrid search embedder returned {len(query_vectors)} "
                f"vectors for one query, falling back to fts-only\n"
            )
            return self._search_fts_only(request)

        # Step 2: SQLite legs. Local + fast usually, but wrap each in
        # the per-leg timeout so a wedged vec0 table or FTS5 tokenizer
        # spin cannot freeze the CLI. On timeout or any other failure:
        # warn + continue with the other leg's results.
        try:
            v_hits = self._run_leg_with_timeout(
                "vector",
                lambda: self._searcher.search_vector(
                    VectorQuery(
                        vector=query_vectors[0],
                        model=self._embedder.model().slug,  # type: ignore[union-attr]
                        limit=over_fetch,
                        scopes=[str(s) for s in request.scopes],
                        kinds=[str(k) for k in request.kinds],
                        # P1: project isolation pushed down to SQL so a
                        # PROJECT actor's vector hits are pre-filtered.
                        # Empty for USER/GLOBAL (no isolation needed).
                        project_id=request.as_project_id
                        if request.as_scope == Scope.PROJECT
                        else "",
                    )
                ),
            )
        except Exception as exc:
            self._warn(
                f"warn: hybrid search vector lookup failed "
                f"(timeout or error), falling back to fts-only: {exc}\n"
            )
            return self._search_fts_only(request)

        try:
            f_hits = self._run_leg_with_timeout(
                "fts",
                lambda: self._searcher.search_fts(
                    SearchQuery(query=request.query, limit=over_fetch)
                ),
            )
        except Exception as exc:
            # Symmetric with the vector-failure path: warn + proceed
            # with what we have. Discarding v_hits would waste the
            # embed + KNN work already done. f_hits becomes empty; the
            # vector loop still scores its hits, and the final trim to
            # limit returns them as matched_by=["vector"]-only.
            self._warn(
                f"warn: hybrid search fts failed "
                f"(timeout or error), continuing with vector-only results: {exc}\n"
            )
            f_hits = []

        # Step 3: RRF fusion. score = Σ 1/(rank + RRF_K).
        return self._fuse(f_hits, v_hits, request, limit)

    def _fuse(
        self,
        f_hits: list[SearchHit],
        v_hits: list[VectorHit],
        request: SearchRequest,
        limit: int,
    ) -> SearchResponse:
        """RRF-merge FTS + vector hits into a single ranked list."""
        # item_id → mutable accumulator. Same shape as Go's `*fusion`.
        fused: dict[str, dict[str, object]] = {}
        filters = _filters_from(request)
        # Cache hydrated items so IDs appearing in both FTS and vector
        # results only trigger one repo.get call.
        item_cache: dict[str, ContextItem] = {}

        # FTS leg. surviving_rank is 0-indexed and only increments when
        # an item passes the scope/kind filter — f_hits comes back
        # UNFILTERED (FTS5 query has no scope/kind predicates), so the
        # raw range index would assign unfairly high ranks to in-scope
        # items that happened to land below out-of-scope items in BM25
        # order. RRF contribution is 1/(surviving_rank+RRF_K), so the
        # top surviving FTS hit contributes 1/60.
        surviving_rank = 0
        for hit in f_hits:
            item = self._hydrate(hit.id, cache=item_cache)
            if item is None:
                continue
            if not filters.accepts(item):
                continue
            entry = fused.setdefault(
                hit.id, {"item": item, "score": 0.0, "snippet": "", "matched_by": []}
            )
            entry["score"] = float(entry["score"]) + 1.0 / (surviving_rank + RRF_K)  # type: ignore[operator]
            entry["matched_by"] = [*entry["matched_by"], "fts"]  # type: ignore[list-item]
            if not entry["snippet"]:
                entry["snippet"] = hit.snippet
            surviving_rank += 1

        # Vector leg. Vector search has no snippet text; fall back to
        # the item title so the UI has something to show. v_hits is
        # already filtered at SQL level via JOIN context_item, so the
        # defensive filter below should be a no-op — but if it ever
        # fires (race between SQL query and item update), we still want
        # post-filter rank semantics so vector and FTS contribute on
        # equal footing.
        surviving_vec_rank = 0
        for hit in v_hits:
            item = self._hydrate(hit.id, cache=item_cache)
            if item is None:
                continue
            if not filters.accepts(item):
                continue
            entry = fused.setdefault(
                hit.id,
                {"item": item, "score": 0.0, "snippet": "", "matched_by": []},
            )
            entry["score"] = float(entry["score"]) + 1.0 / (surviving_vec_rank + RRF_K)  # type: ignore[operator]
            entry["matched_by"] = [*entry["matched_by"], "vector"]  # type: ignore[list-item]
            if not entry["snippet"]:
                entry["snippet"] = item.title
            surviving_vec_rank += 1

        out: list[SearchResult] = [
            SearchResult(
                item=entry["item"],  # type: ignore[arg-type]
                score=float(entry["score"]),  # type: ignore[arg-type]
                snippet=str(entry["snippet"]),  # type: ignore[arg-type]
                matched_by=_dedupe_strings(entry["matched_by"]),  # type: ignore[arg-type]
            )
            for entry in fused.values()
        ]
        out.sort(key=lambda r: (-r.score, _id_desc_key(r.item.id)))

        if len(out) > limit:
            out = out[:limit]
        return SearchResponse(results=out, total=len(out))

    def _run_leg_with_timeout(self, leg_name: str, fn: Callable[[], _T]) -> _T:
        """Run a hybrid leg (FTS or vector) with a hard timeout.

        Mirrors Go's ``context.WithTimeout(ctx, s.legTimeoutOrDefault())``
        per leg (search.go:241, 252). Python is sync, so we run the leg
        in a one-worker :class:`ThreadPoolExecutor` and bound its result
        with :meth:`Future.result(timeout=...)`. On timeout the executor
        is shut down without waiting — the worker thread is left to
        finish (or hang) on its own; we just stop blocking on it.

        Raises ``TimeoutError`` on timeout; re-raises any exception the
        leg itself raised. The hybrid path's existing per-leg fallbacks
        catch both.
        """
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(fn)
            try:
                return future.result(timeout=self._leg_timeout)
            except FuturesTimeoutError as exc:
                raise TimeoutError(
                    f"{leg_name} leg exceeded {self._leg_timeout}s timeout"
                ) from exc

    # ---- helpers -----------------------------------------------------

    def _hydrate(
        self,
        item_id: str,
        cache: dict[str, ContextItem] | None,
    ) -> ContextItem | None:
        """Fetch an item by id, caching in `cache` if non-None.

        Returns None on ItemNotFound — the item was deleted between the
        FTS/vector row being written and now; caller skips. Not cached
        (a later retry would still 404).
        """
        if cache is not None and item_id in cache:
            return cache[item_id]
        try:
            item = self._repo.get(item_id)
        except Exception:
            return None
        if cache is not None:
            cache[item_id] = item
        return item

    def _warn(self, msg: str) -> None:
        """Write a warning line to the injected log. Best-effort."""
        with contextlib.suppress(Exception):
            self._log.write(msg)


def _id_desc_key(item_id: str) -> str:
    """Sort key helper: returns the id negated for descending order.

    Python's sorted is stable but doesn't natively do per-field reverse.
    We negate score (use -score) for descending, and for the id tiebreak
    we want descending too (lexically larger id wins). Returning the
    raw id and using a tuple `(-score, id)` would sort id ascending —
    the opposite of what we want.

    Trick: return a 'negated' string by inverting each character's code
    point relative to chr(255). Lexically smaller negated string =
    lexically larger original string. This gives us descending id order
    on a tie.

    ULIDs are ASCII monotonically-increasing, so lexical inversion is
    well-defined. The 255-offset keeps the inversion within printable
    ASCII range (ULID chars are 0-9, A-Z).
    """
    return "".join(chr(255 - ord(c)) for c in item_id)
