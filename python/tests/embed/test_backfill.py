"""Tests for BackfillService — bulk embed items where any_embedding=0.

Mirrors Go's backfill_test.go. Tests use a FilteringFakeRepo (defined
below) that honors ItemFilter.any_embedding + ItemFilter.not_done_for_model
so we can verify the pre-filter behavior without spinning up sqlite.
Filter behavior itself is tested at the storage/ layer.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from io import StringIO

from tests._fakes.canned_filestore import CannedFileStore
from tests._fakes.fake_embedder import FakeEmbedder
from unictx.embed.backfill import BackfillService
from unictx.embed.embedder import ModelInfo
from unictx.embed.service import EmbedService
from unictx.items.errors import ItemNotFound
from unictx.items.models import ContextItem, Kind, Scope, Source
from unictx.items.repo import ItemFilter

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _StubVectorStore:
    put_calls: list[tuple[str, str, list[float]]] = field(default_factory=list)
    err_for: Callable[[str], Exception | None] | None = None

    def put(self, model: str, item_id: str, vector: list[float]) -> None:
        if self.err_for is not None:
            err = self.err_for(item_id)
            if err is not None:
                raise err
        self.put_calls.append((model, item_id, list(vector)))

    def search(self, q): raise NotImplementedError

    def delete(self, model: str, item_id: str) -> None: ...


@dataclass(slots=True)
class _RecordingEmbRepo:
    calls: list[tuple[str, str, str, str]] = field(default_factory=list)

    def upsert_status(self, item_id, model_slug, status, err_str):
        self.calls.append((item_id, model_slug, status, err_str))

    def list_failed(self, limit: int): return []
    def get_status(self, item_id, model_slug): raise KeyError
    def list_for_item(self, item_id): return []


class _FilteringRepo:
    """Composition-based ContextRepo stub that honors ItemFilter in list().

    FakeContextRepo is ``@dataclass(slots=True)`` — subclassing + adding
    a new field (emb_repo for not_done_for_model lookups) is awkward
    with slots inheritance. Composition is cleaner; we explicitly
    delegate the Protocol methods.

    Honors:
      - ``any_embedding``: 0/1/None filter (None = no filter).
      - ``not_done_for_model``: needs emb_repo to look up done rows.
      - ``limit``: caps the result list (>0 only).
    """

    def __init__(self, items: dict[str, ContextItem] | None = None,
                 emb_repo: _RecordingEmbRepo | None = None) -> None:
        self.items: dict[str, ContextItem] = items or {}
        self._emb_repo = emb_repo

    def create(self, item: ContextItem) -> None:
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
        items = list(self.items.values())
        if filter.any_embedding is not None:
            items = [i for i in items if i.any_embedding == filter.any_embedding]
        if filter.not_done_for_model and self._emb_repo is not None:
            done = {
                (c[0], c[1])
                for c in self._emb_repo.calls
                if c[2] == "done"
            }
            items = [
                i for i in items
                if (i.id, filter.not_done_for_model) not in done
            ]
        if filter.limit > 0:
            items = items[: filter.limit]
        return items, ""

    def next_cursor(self, item: ContextItem) -> str:
        return ""

    def reindex_fts(self, *args): ...


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Fixture:
    repo: _FilteringRepo
    emb_repo: _RecordingEmbRepo
    vs: _StubVectorStore
    log: StringIO

    def make_service(self, embedder=None) -> EmbedService:
        return EmbedService(
            embedder or FakeEmbedder(
                dimension=8, model_info=ModelInfo(slug="fake-model", dimension=8)
            ),
            self.vs, self.repo, CannedFileStore(), self.emb_repo, log=StringIO(),
        )


def _make_fixture() -> _Fixture:
    emb_repo = _RecordingEmbRepo()
    return _Fixture(
        repo=_FilteringRepo(emb_repo=emb_repo),
        emb_repo=emb_repo,
        vs=_StubVectorStore(),
        log=StringIO(),
    )


def _make_item(item_id: str, title: str, content: str, any_embedding: int = 0) -> ContextItem:
    item = ContextItem(
        id=item_id,
        scope=Scope.USER, kind=Kind.NOTE, source=Source.MANUAL,
        owner_user_id="u-1",
        title=title, content=content,
    )
    item.any_embedding = any_embedding
    return item


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_processes_only_unembedded_items() -> None:
    """Items with any_embedding=1 are filtered out before iteration."""
    f = _make_fixture()
    a = _make_item("a", "alpha", "A")
    b = _make_item("b", "beta", "B")
    c = _make_item("c", "gamma", "C", any_embedding=1)  # already embedded
    for item in (a, b, c):
        f.repo.create(item)

    svc = f.make_service()
    backfill = BackfillService(f.repo, svc, log=f.log)
    report = backfill.run()

    assert report.embedded == 2, "only A and B embedded; C excluded by filter"
    assert report.failed == 0
    # Both A and B have status='done'.
    a_dones = [c for c in f.emb_repo.calls if c[0] == "a" and c[2] == "done"]
    b_dones = [c for c in f.emb_repo.calls if c[0] == "b" and c[2] == "done"]
    assert a_dones, "A must have a status='done' row"
    assert b_dones, "B must have a status='done' row"


def test_dry_run_does_not_embed() -> None:
    """dry_run=True → increment scanned only; no embed calls."""
    f = _make_fixture()
    f.repo.create(_make_item("a", "alpha", "A"))

    svc = f.make_service()
    backfill = BackfillService(f.repo, svc, log=f.log)
    report = backfill.run(dry_run=True)

    assert report.embedded == 0
    assert report.scanned == 1
    # No status row written during dry run.
    assert f.emb_repo.calls == []


def test_limit_honored() -> None:
    """limit caps the number of items processed."""
    f = _make_fixture()
    for title in ("a", "b", "c", "d", "e"):
        f.repo.create(_make_item(title, title, f"content {title}"))

    svc = f.make_service()
    backfill = BackfillService(f.repo, svc, log=f.log)
    report = backfill.run(limit=3)

    assert report.embedded == 3
    assert report.scanned == 3


def test_continues_on_embed_failure() -> None:
    """Per-item embed error → recorded in failures, run continues."""
    f = _make_fixture()
    f.repo.create(_make_item("a", "alpha", "A"))
    f.repo.create(_make_item("b", "beta", "B"))  # this one will fail
    f.repo.create(_make_item("c", "gamma", "C"))

    # VectorStore.put raises for "b" only.
    f.vs.err_for = lambda iid: RuntimeError("simulated failure on b") if iid == "b" else None
    svc = f.make_service()
    backfill = BackfillService(f.repo, svc, log=f.log)

    report = backfill.run()

    assert report.embedded == 2, "A and C embedded"
    assert report.failed == 1, "B failed"
    assert len(report.failures) == 1
    assert report.failures[0].item_id == "b"
    assert "simulated failure on b" in report.failures[0].error


def test_stop_event_returns_partial_report() -> None:
    """stop_event set mid-iteration → return early with partial report."""
    f = _make_fixture()
    for title in ("a", "b", "c"):
        f.repo.create(_make_item(title, title, title))

    svc = f.make_service()
    backfill = BackfillService(f.repo, svc, log=f.log)

    stop = threading.Event()
    stop.set()  # pre-set → no work done

    report = backfill.run(stop_event=stop)
    assert report.scanned == 0
    assert report.embedded == 0
