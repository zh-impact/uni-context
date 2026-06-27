"""Tests for ReembedService — re-embed under active model.

Mirrors Go's reembed_test.go. The key contract: filter by items lacking
a status='done' row for ``active.slug``. Tests share the _FilteringRepo
pattern from test_backfill (re-defined here so this file is standalone).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from io import StringIO

from tests._fakes.canned_filestore import CannedFileStore
from tests._fakes.fake_embedder import FakeEmbedder
from unictx.embed.embedder import ModelInfo
from unictx.embed.reembed import ReembedService
from unictx.embed.service import EmbedService
from unictx.items.errors import ItemNotFound
from unictx.items.models import ContextItem, Kind, Scope, Source
from unictx.items.repo import ItemFilter

# ---------------------------------------------------------------------------
# Test doubles (mirrors test_backfill; kept independent for file locality)
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
    """Composition-based ContextRepo stub that honors ItemFilter.

    See test_backfill.py for design rationale (FakeContextRepo's slots
    make subclassing awkward).
    """

    def __init__(self, items: dict[str, ContextItem] | None = None,
                 emb_repo: _RecordingEmbRepo | None = None) -> None:
        self.items: dict[str, ContextItem] = items or {}
        self._emb_repo = emb_repo

    def create(self, item): self.items[item.id] = item

    def get(self, id):
        try:
            return self.items[id]
        except KeyError as exc:
            raise ItemNotFound(id) from exc

    def update(self, item):
        if item.id not in self.items:
            raise ItemNotFound(item.id)
        self.items[item.id] = item
        return item

    def delete(self, id):
        if id not in self.items:
            raise ItemNotFound(id)
        del self.items[id]

    def list(self, filter: ItemFilter) -> tuple[list[ContextItem], str]:
        items = list(self.items.values())
        if filter.any_embedding is not None:
            items = [i for i in items if i.any_embedding == filter.any_embedding]
        if filter.not_done_for_model and self._emb_repo is not None:
            done = {(c[0], c[1]) for c in self._emb_repo.calls if c[2] == "done"}
            items = [
                i for i in items
                if (i.id, filter.not_done_for_model) not in done
            ]
        if filter.limit > 0:
            items = items[: filter.limit]
        return items, ""

    def next_cursor(self, item): return ""
    def reindex_fts(self, *args): ...


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _make_item(item_id: str, title: str, content: str) -> ContextItem:
    return ContextItem(
        id=item_id,
        scope=Scope.USER, kind=Kind.NOTE, source=Source.MANUAL,
        owner_user_id="u-1",
        title=title, content=content,
    )


@dataclass(slots=True)
class _Fixture:
    repo: _FilteringRepo
    emb_repo: _RecordingEmbRepo
    vs: _StubVectorStore
    log: StringIO

    def make_service(self, slug: str = "fake-model") -> EmbedService:
        return EmbedService(
            FakeEmbedder(dimension=8, model_info=ModelInfo(slug=slug, dimension=8)),
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reembed_against_new_model() -> None:
    """Items done under old model are re-embedded under active model.

    Setup: 2 items previously marked 'done' under 'old-model'.
    Switch active to 'new-model'. ReembedService should pick up both
    (they lack a done row for 'new-model') and embed them.
    """
    f = _make_fixture()
    f.repo.create(_make_item("a", "alpha", "A"))
    f.repo.create(_make_item("b", "beta", "B"))
    # Pretend both were previously done under 'old-model'.
    f.emb_repo.calls.append(("a", "old-model", "done", ""))
    f.emb_repo.calls.append(("b", "old-model", "done", ""))

    active = ModelInfo(slug="new-model", dimension=8)
    svc = f.make_service(slug="new-model")
    reembed = ReembedService(f.repo, svc, active, log=f.log)
    report = reembed.run()

    assert report.embedded == 2, "both items re-embedded under new-model"
    assert report.failed == 0
    # Status rows written under new-model.
    new_dones = [c for c in f.emb_repo.calls if c[1] == "new-model" and c[2] == "done"]
    assert len(new_dones) == 2


def test_dry_run_does_not_embed() -> None:
    """dry_run=True → increment scanned only."""
    f = _make_fixture()
    f.repo.create(_make_item("a", "alpha", "A"))

    active = ModelInfo(slug="fake-model", dimension=8)
    svc = f.make_service()
    reembed = ReembedService(f.repo, svc, active, log=f.log)
    report = reembed.run(dry_run=True)

    assert report.embedded == 0
    assert report.scanned == 1
    # No embed calls (no new status rows).
    assert all(c[1] != "fake-model" for c in f.emb_repo.calls), (
        "dry run must not write any status rows"
    )


def test_already_done_for_active_model_skipped() -> None:
    """Items with status='done' for active.slug are filtered out."""
    f = _make_fixture()
    a = _make_item("a", "alpha", "A")
    b = _make_item("b", "beta", "B")
    f.repo.create(a)
    f.repo.create(b)
    # 'a' already done for active model; 'b' is not.
    f.emb_repo.calls.append(("a", "fake-model", "done", ""))

    active = ModelInfo(slug="fake-model", dimension=8)
    svc = f.make_service()
    reembed = ReembedService(f.repo, svc, active, log=f.log)
    report = reembed.run()

    assert report.embedded == 1, "only b needs re-embed"
    assert report.scanned == 1


def test_continues_on_embed_failure() -> None:
    """Per-item failure recorded, run continues."""
    f = _make_fixture()
    f.repo.create(_make_item("a", "alpha", "A"))
    f.repo.create(_make_item("b", "beta", "B"))  # this one fails

    f.vs.err_for = lambda iid: RuntimeError("persistent") if iid == "b" else None
    active = ModelInfo(slug="fake-model", dimension=8)
    svc = f.make_service()
    reembed = ReembedService(f.repo, svc, active, log=f.log)

    report = reembed.run()
    assert report.embedded == 1
    assert report.failed == 1
    assert report.failures[0].item_id == "b"


def test_limit_honored() -> None:
    """limit caps the iteration."""
    f = _make_fixture()
    for title in ("a", "b", "c", "d", "e"):
        f.repo.create(_make_item(title, title, title))

    active = ModelInfo(slug="fake-model", dimension=8)
    svc = f.make_service()
    reembed = ReembedService(f.repo, svc, active, log=f.log)
    report = reembed.run(limit=3)
    assert report.embedded == 3
    assert report.scanned == 3


def test_stop_event_returns_partial_report() -> None:
    """stop_event pre-set → no items processed."""
    f = _make_fixture()
    f.repo.create(_make_item("a", "alpha", "A"))

    active = ModelInfo(slug="fake-model", dimension=8)
    svc = f.make_service()
    reembed = ReembedService(f.repo, svc, active, log=f.log)

    stop = threading.Event()
    stop.set()
    report = reembed.run(stop_event=stop)
    assert report.scanned == 0
    assert report.embedded == 0
