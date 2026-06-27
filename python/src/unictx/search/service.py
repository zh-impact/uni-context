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
  - Go's ``context.WithTimeout`` per leg dropped (Python is sync; the
    embed call is bounded by ``httpx.Client.timeout``, the SQLite legs
    are local). The ``leg_timeout`` field is retained for forward-compat
    but unused on the SQLite path; embed timeout comes from the embedder.
  - ``log`` defaults to ``sys.stderr``; tests pass ``StringIO``.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from typing import IO

from unictx.embed.embedder import Embedder
from unictx.items.models import ContextItem, Kind, Scope
from unictx.items.repo import ContextRepo
from unictx.search.searcher import Searcher, SearchHit, SearchMode, SearchQuery
from unictx.search.vectorstore import VectorHit, VectorQuery

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

# Default per-leg timeout when leg_timeout is None. Currently unused on
# the SQLite path (queries are local), retained for forward-compat.
_DEFAULT_LEG_TIMEOUT = 5.0


@dataclass(slots=True)
class SearchRequest:
    """Inputs to a SearchService.search call. Mirrors Go's SearchRequest."""

    query: str = ""
    scopes: list[Scope] = field(default_factory=list)
    kinds: list[Kind] = field(default_factory=list)
    limit: int = 0
    mode: SearchMode = SearchMode.FTS_ONLY


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
    """Scope/kind acceptance sets built from a SearchRequest.

    None means "all match" — an empty scopes list produces a filter
    that accepts every scope.
    """

    scopes: dict[Scope, bool] | None = None
    kinds: dict[Kind, bool] | None = None

    def accepts(self, item: ContextItem) -> bool:
        if self.scopes is not None and item.scope not in self.scopes:
            return False
        return not (self.kinds is not None and item.kind not in self.kinds)


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
    """

    def __init__(
        self,
        searcher: Searcher,
        repo: ContextRepo,
        log: IO[str] | None = None,
        *,
        embedder: Embedder | None = None,
        leg_timeout: float | None = None,
    ) -> None:
        self._searcher = searcher
        self._repo = repo
        self._log: IO[str] = log if log is not None else sys.stderr
        self._embedder: Embedder | None = embedder
        self._leg_timeout: float = (
            leg_timeout if leg_timeout and leg_timeout > 0 else _DEFAULT_LEG_TIMEOUT
        )

    def search(self, request: SearchRequest) -> SearchResponse:
        """Dispatch by request.mode. Hybrid without embedder → fts-only."""
        if request.mode == SearchMode.HYBRID:
            if self._embedder is None:
                # Hybrid requested but no embedder wired — degrade to
                # fts-only. Plan 1 callers that never set mode are also
                # here (their mode defaults to FTS_ONLY in the dataclass).
                return self._search_fts_only(request)
            return self._search_hybrid(request)
        return self._search_fts_only(request)

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

        filters = _Filters(
            scopes=_scope_set(request.scopes),
            kinds=_kind_set(request.kinds),
        )

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

        # Step 2: SQLite legs. Local + fast, no timeout wrapper.
        # On per-leg failure: warn + continue with the other leg.
        try:
            v_hits = self._searcher.search_vector(
                VectorQuery(
                    vector=query_vectors[0],
                    model=self._embedder.model().slug,  # type: ignore[union-attr]
                    limit=over_fetch,
                    scopes=[str(s) for s in request.scopes],
                    kinds=[str(k) for k in request.kinds],
                )
            )
        except Exception as exc:
            self._warn(
                f"warn: hybrid search vector lookup failed, "
                f"falling back to fts-only: {exc}\n"
            )
            return self._search_fts_only(request)

        try:
            f_hits = self._searcher.search_fts(
                SearchQuery(query=request.query, limit=over_fetch)
            )
        except Exception as exc:
            # Symmetric with the vector-failure path: warn + proceed
            # with what we have. Discarding v_hits would waste the
            # embed + KNN work already done. f_hits becomes empty; the
            # vector loop still scores its hits, and the final trim to
            # limit returns them as matched_by=["vector"]-only.
            self._warn(
                f"warn: hybrid search fts failed, "
                f"continuing with vector-only results: {exc}\n"
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
        filters = _Filters(
            scopes=_scope_set(request.scopes),
            kinds=_kind_set(request.kinds),
        )
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
