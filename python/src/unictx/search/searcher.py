"""Searcher Protocol + SearchHit/SearchQuery/SearchMode.

Ports Go's internal/port/searcher.go. Adaptations per Plan
§Python Conventions: ctx params dropped, snake_case methods,
`@dataclass(slots=True)` for value types, StrEnum for closed-set enums.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Forward references to search/vectorstore.py types used in
    # Searcher.search_vector's signature. TYPE_CHECKING-only because
    # search/vectorstore.py does not import this module — no runtime
    # cycle — but importing at runtime would still pull vectorstore's
    # dependencies unnecessarily. Annotations are strings (PEP 563).
    from unictx.search.vectorstore import VectorHit, VectorQuery


class SearchMode(StrEnum):
    """Closed set of search strategies.

    Mirrors the implicit mode selection in Go's service.Search wrapper.
    FTS_ONLY = pure BM25 keyword search. HYBRID = BM25 + KNN vector
    search, results merged by the impl.
    """

    FTS_ONLY = "fts_only"
    HYBRID = "hybrid"


@dataclass(slots=True)
class SearchQuery:
    """Inputs to a keyword search. Mirrors Go's port.SearchQuery.

    Future expansion (filter by scope/kind/tags via FTS WHERE) lives in
    the service.Search wrapper, not here — keeps the Protocol minimal.
    """

    query: str = ""
    limit: int = 0


@dataclass(slots=True)
class SearchHit:
    """One BM25 search result. Mirrors Go's port.SearchHit."""

    id: str = ""
    score: float = 0.0
    snippet: str = ""


@runtime_checkable
class Searcher(Protocol):
    """Keyword + vector search. Mirrors Go's port.Searcher.

    Implementations may delegate search_vector to a separate VectorStore
    (see sqlite.Searcher, which composes both). The two methods are
    intentionally separate so callers can do BM25-only cheaply without
    paying the KNN cost.
    """

    def search_fts(self, q: SearchQuery) -> list[SearchHit]:
        """BM25 keyword search via FTS5. Returns hits sorted by score DESC."""
        ...

    def search_vector(self, q: VectorQuery) -> list[VectorHit]:
        """KNN vector search against the backing vector store.

        Returns hits ordered by score DESC. VectorQuery/VectorHit are
        defined in search/vectorstore.py (TYPE_CHECKING import above;
        annotations are strings under PEP 563 so no runtime cycle).
        """
        ...
