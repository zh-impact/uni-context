"""FakeContextRepo — in-memory ContextRepo for service-layer tests.

Ports Go's internal/service/fake_repo_test.go semantics. The Go fake
lives in the service/ package (Go test-package scoping forces that);
in Python it goes in tests/_fakes/ as a shared stub.

Differences from Go:
  - Methods raise Python errors instead of returning (T, error):
      * get/update/delete on missing id -> ItemNotFound
      * create on duplicate id -> ItemValidationError
  - list returns (rows, "") ignoring ItemFilter (Go does the same).
  - reindex_fts records the call; the count is exposed via
    `reindex_fts_calls` for rollback-contract verification in Phase 5.

The class is intentionally NOT thread-safe. Service-layer tests run
synchronously; adding a Lock would obscure the test's intent. If a
later test needs concurrency, add a Lock then.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from unictx.items.errors import ItemNotFound, ItemValidationError
from unictx.items.models import ContextItem
from unictx.items.repo import ItemFilter


@dataclass(slots=True)
class FakeContextRepo:
    """Dict-backed ContextRepo. Records reindex_fts for rollback tests.

    Attributes:
      items: id -> ContextItem. Tests can mutate this directly to seed.
      reindex_fts_calls: number of times reindex_fts was invoked.
        IngestService tests assert this counter to verify the
        externalized-content reindex path is taken.
      reindex_fts_args: list of (id, title, summary, content) tuples,
        in call order. Useful when more than one item is reindexed
        and the test wants to check ordering or arguments.
      create_err: when non-None, create() raises this instead of
        inserting. Lets tests inject a Create failure to exercise
        rollback paths in IngestService (Phase 5).
    """

    items: dict[str, ContextItem] = field(default_factory=dict)
    reindex_fts_calls: int = 0
    reindex_fts_args: list[tuple[str, str, str, str]] = field(default_factory=list)
    create_err: Exception | None = None

    def create(self, item: ContextItem) -> None:
        if self.create_err is not None:
            raise self.create_err
        if item.id in self.items:
            raise ItemValidationError(f"duplicate id: {item.id}")
        self.items[item.id] = item

    def get(self, id: str) -> ContextItem:
        try:
            return self.items[id]
        except KeyError as exc:
            raise ItemNotFound(id) from exc

    def update(self, item: ContextItem) -> ContextItem:
        if item.id not in self.items:
            raise ItemNotFound(item.id)
        self.items[item.id] = item
        return item

    def delete(self, id: str) -> None:
        if id not in self.items:
            raise ItemNotFound(id)
        del self.items[id]

    def list(self, filter: ItemFilter) -> tuple[list[ContextItem], str]:
        # Go's fake returns everything ignoring the filter; preserve that
        # so service tests don't have to construct filters to exercise
        # code paths that read via list().
        return list(self.items.values()), ""

    def next_cursor(self, item: ContextItem) -> str:
        # Go returns "" — no pagination in the fake. Tests that need
        # pagination semantics talk to the real storage/ impl.
        return ""

    def reindex_fts(self, id: str, title: str, summary: str, content: str) -> None:
        self.reindex_fts_calls += 1
        self.reindex_fts_args.append((id, title, summary, content))
