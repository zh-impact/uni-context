"""Composite adapter that satisfies the Searcher Protocol.

Phase 2 split FTS + vector search across two impls:
  - :class:`unictx.storage.searcher_impl.SearcherImpl` — FTS5 + LIKE
    fallback (the ``search(query, limit)`` method).
  - :class:`unictx.storage.vectorstore_impl.VectorStoreImpl` — vec0 KNN
    (the ``search(vector, model_slug, ...)`` method).

Phase 1's :class:`unictx.search.searcher.Searcher` Protocol, however,
requires BOTH ``search_fts`` and ``search_vector`` on the same object.
This adapter composes the two Phase 2 impls into an object that
satisfies the Protocol, so Phase 5's SearchService can take a single
Searcher dependency.

Without this adapter, SearchService would have to take two separate
dependencies (FTS + vector) — a Phase 1 contract drift. The adapter
keeps the Searcher Protocol as the single dependency surface.

Used by :func:`unictx.cli.app.wire` to compose the storage layer into
the SearchService. Production-only; tests of SearchService itself use
fakes that already satisfy the Protocol.
"""

from __future__ import annotations

from unictx.search.searcher import SearchHit, SearchQuery
from unictx.search.vectorstore import VectorHit, VectorQuery
from unictx.storage.searcher_impl import SearcherImpl
from unictx.storage.vectorstore_impl import VectorStoreImpl

__all__ = ["CompositeSearcher"]


class CompositeSearcher:
    """Adapts (SearcherImpl, VectorStoreImpl) → Searcher Protocol.

    The two underlying impls share the same DB connection (passed
    independently to each constructor by the wire layer). No state is
    held beyond the two references; the adapter is stateless.
    """

    __slots__ = ("_fts", "_vs")

    def __init__(self, fts: SearcherImpl, vs: VectorStoreImpl) -> None:
        self._fts = fts
        self._vs = vs

    def search_fts(self, q: SearchQuery) -> list[SearchHit]:
        """BM25 keyword search via FTS5 (+ LIKE fallback for short queries).

        Delegates to ``SearcherImpl.search(query, limit)`` and translates
        the storage-side ``SearchHit`` (which carries ``title_snip`` —
        a title-only snippet per the externalized-content corruption
        bugfix in ``searcher_impl.py``) into the canonical
        ``search.searcher.SearchHit`` (which carries ``snippet``).
        """
        storage_hits = self._fts.search(q.query, q.limit)
        return [SearchHit(id=h.id, score=h.score, snippet=h.title_snip) for h in storage_hits]

    def search_vector(self, q: VectorQuery) -> list[VectorHit]:
        """vec0 KNN search via VectorStoreImpl.

        Mirrors the Phase 2 signature: positional ``vector`` + ``model``
        + ``limit``, keyword-only ``scopes`` + ``kinds``.
        """
        return self._vs.search(
            q.vector,
            q.model,
            q.limit,
            scopes=q.scopes,
            kinds=q.kinds,
        )
