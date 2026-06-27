"""Tests for WorkerService — polls failed embeddings + retries.

Mirrors Go's worker_test.go. The fixture wires a real EmbedService
against FakeEmbedder + StubVectorStore + RecordingEmbeddingRepo +
FakeContextRepo. Tests seed failed rows by calling EmbedService with
ErrorEmbedder, then swap in a working embedder and call
``run_one_iteration`` to verify retry semantics.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from io import StringIO

import pytest

from tests._fakes.canned_filestore import CannedFileStore
from tests._fakes.fake_embedder import ErrorEmbedder, FakeEmbedder
from tests._fakes.fake_repo import FakeContextRepo
from unictx.embed.embedder import ModelInfo
from unictx.embed.service import EmbedService
from unictx.embed.worker import WorkerService
from unictx.items.models import ContextItem, Kind, Scope, Source

# ---------------------------------------------------------------------------
# Test doubles (reuse from test_service.py shape; defined fresh to keep
# this file standalone).
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _StubVectorStore:
    put_calls: list[tuple[str, str, list[float]]] = field(default_factory=list)
    data: dict[tuple[str, str], list[float]] = field(default_factory=dict)

    def put(self, model: str, item_id: str, vector: list[float]) -> None:
        self.put_calls.append((model, item_id, list(vector)))
        self.data[(model, item_id)] = list(vector)

    def search(self, q): raise NotImplementedError

    def delete(self, model: str, item_id: str) -> None:
        self.data.pop((model, item_id), None)


@dataclass(slots=True)
class _RecordingEmbRepo:
    calls: list[tuple[str, str, str, str]] = field(default_factory=list)

    def upsert_status(self, item_id, model_slug, status, err_str):
        self.calls.append((item_id, model_slug, status, err_str))

    def list_failed(self, limit: int):
        # Return rows whose latest status is 'failed', embedded_at ASC.
        # Aggregate the calls dict to find each item's latest status.
        latest: dict[str, tuple[str, str, str, str]] = {}
        for call in self.calls:
            item_id, model_slug, status, err_str = call
            latest[item_id] = call  # later overwrites earlier (preserves order)
        # Order by first-seen so tests get deterministic output.
        first_seen: dict[str, int] = {}
        for idx, call in enumerate(self.calls):
            iid = call[0]
            if iid not in first_seen:
                first_seen[iid] = idx
        rows = []
        for iid, call in latest.items():
            if call[2] == "failed":
                attempts = sum(1 for c in self.calls if c[0] == iid)
                rows.append(_StubStatus(
                    item_id=iid, model_slug=call[1], attempts=attempts,
                ))
        rows.sort(key=lambda r: first_seen.get(r.item_id, 0))
        return rows[:limit] if limit > 0 else rows

    def get_status(self, item_id, model_slug):
        for call in reversed(self.calls):
            if call[0] == item_id and call[1] == model_slug:
                return _StubStatus(
                    item_id=call[0], model_slug=call[1],
                    status=call[2], error=call[3], last_error=call[3],
                    attempts=sum(1 for c in self.calls if c[0] == item_id),
                )
        raise KeyError(f"no status for {item_id}/{model_slug}")

    def list_for_item(self, item_id):
        raise NotImplementedError


@dataclass(slots=True)
class _StubStatus:
    """One row in context_embedding (status-only stub)."""
    item_id: str = ""
    model_slug: str = ""
    status: str = ""
    error: str = ""
    last_error: str = ""
    attempts: int = 0
    embedded_at: int = 0


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Fixture:
    repo: FakeContextRepo
    emb_repo: _RecordingEmbRepo
    vs: _StubVectorStore
    log: StringIO
    embedder: FakeEmbedder

    def make_service(self) -> EmbedService:
        """Build EmbedService wired with the live fixture components."""
        return EmbedService(
            self.embedder, self.vs, self.repo,
            CannedFileStore(), self.emb_repo, log=StringIO(),
        )


def _make_fixture(slug: str = "fake-model") -> _Fixture:
    embedder = FakeEmbedder(dimension=8, model_info=ModelInfo(slug=slug, dimension=8))
    return _Fixture(
        repo=FakeContextRepo(),
        emb_repo=_RecordingEmbRepo(),
        vs=_StubVectorStore(),
        log=StringIO(),
        embedder=embedder,
    )


def _make_item(item_id: str, title: str, content: str) -> ContextItem:
    return ContextItem(
        id=item_id,
        scope=Scope.USER, kind=Kind.NOTE, source=Source.MANUAL,
        owner_user_id="u-1",
        title=title, content=content,
    )


# ---------------------------------------------------------------------------
# run_one_iteration
# ---------------------------------------------------------------------------


def test_retries_failed_embeddings() -> None:
    """Three failed items → retry succeeds → all become 'done'."""
    f = _make_fixture()
    # Initial failures via ErrorEmbedder.
    err_emb = ErrorEmbedder(inner=f.embedder, reason="transient")
    failing_svc = EmbedService(
        err_emb, f.vs, f.repo, CannedFileStore(), f.emb_repo, log=StringIO()
    )
    for i, title in enumerate(["alpha", "beta", "gamma"]):
        item = _make_item(f"id-{i}", title, "content")
        f.repo.create(item)
        with pytest.raises(RuntimeError, match=r"embed item"):
            failing_svc.embed_item(item.id, title, "content")

    # Verify all 3 are 'failed' attempts=1.
    for i in range(3):
        st = f.emb_repo.get_status(f"id-{i}", "fake-model")
        assert st.status == "failed"
        assert st.attempts == 1

    # Swap to working embedder; run worker one iteration.
    worker = WorkerService(f.repo, f.emb_repo, f.make_service(), log=f.log)
    processed = worker.run_one_iteration()

    assert processed == 3, "all 3 failures retried"
    # All 3 now 'done' with attempts=2 (1 fail + 1 success).
    for i in range(3):
        st = f.emb_repo.get_status(f"id-{i}", "fake-model")
        assert st.status == "done"
        assert st.attempts == 2


def test_no_failures_returns_zero() -> None:
    """No failed rows → processed=0."""
    f = _make_fixture()
    worker = WorkerService(f.repo, f.emb_repo, f.make_service(), log=f.log)
    assert worker.run_one_iteration() == 0


def test_partial_failure_keeps_item_in_queue() -> None:
    """Mixed: one item keeps failing, one succeeds → both processed once."""
    f = _make_fixture()
    # Initial failures.
    err_emb = ErrorEmbedder(inner=f.embedder, reason="init fail")
    failing_svc = EmbedService(
        err_emb, f.vs, f.repo, CannedFileStore(), f.emb_repo, log=StringIO()
    )
    item_fail = _make_item("id-fail", "fail-title", "content F")
    item_ok = _make_item("id-ok", "ok-title", "content S")
    f.repo.create(item_fail)
    f.repo.create(item_ok)
    for item in (item_fail, item_ok):
        with pytest.raises(RuntimeError, match=r"embed item"):
            failing_svc.embed_item(item.id, item.title, item.content)

    # Mixed embedder: succeed for ok-title, fail otherwise.
    class _MixedEmbed:
        def model(self): return ModelInfo(slug="fake-model", dimension=8)
        def embed(self, texts):
            out = []
            for t in texts:
                if "ok-title" in t:
                    out.append([0.0] * 8)
                else:
                    raise RuntimeError("persistent")
            return out

    mixed_emb = _MixedEmbed()
    mixed_svc = EmbedService(
        mixed_emb, f.vs, f.repo, CannedFileStore(), f.emb_repo, log=StringIO()
    )
    worker = WorkerService(f.repo, f.emb_repo, mixed_svc, log=f.log)
    processed = worker.run_one_iteration()
    assert processed == 2

    st_fail = f.emb_repo.get_status("id-fail", "fake-model")
    assert st_fail.status == "failed"
    assert st_fail.attempts == 2

    st_ok = f.emb_repo.get_status("id-ok", "fake-model")
    assert st_ok.status == "done"
    assert st_ok.attempts == 2


def test_item_vanished_between_failure_and_retry_logs_and_skips() -> None:
    """Item deleted after failure recorded → log warn + skip (not raise)."""
    f = _make_fixture()
    # Seed a failed status row directly.
    f.emb_repo.calls.append(("ghost", "fake-model", "failed", "init err"))

    worker = WorkerService(f.repo, f.emb_repo, f.make_service(), log=f.log)
    processed = worker.run_one_iteration()

    # No items processed (the vanished item was skipped).
    assert processed == 0
    # Warning logged.
    log_text = f.log.getvalue()
    assert "ghost" in log_text
    assert "vanished" in log_text


# ---------------------------------------------------------------------------
# run (loop)
# ---------------------------------------------------------------------------


def test_run_returns_immediately_on_pre_set_event() -> None:
    """Pre-set stop_event → run returns without doing any work.

    Mirrors Go's pre-cancelled ctx short-circuit (worker.go:95-100).
    """
    f = _make_fixture()
    worker = WorkerService(f.repo, f.emb_repo, f.make_service(), log=f.log)
    stop = threading.Event()
    stop.set()

    # Pre-set event: run returns immediately. No progress log written.
    worker.run(interval=0.01, stop_event=stop)
    assert "processed" not in f.log.getvalue(), (
        "pre-set event must short-circuit before first iteration"
    )


def test_run_loops_until_event_set() -> None:
    """run loops and exits when stop_event is set mid-sleep.

    Uses a short interval + setting the event from another thread to
    verify the loop is responsive mid-sleep (via stop_event.wait).
    """
    f = _make_fixture()
    worker = WorkerService(f.repo, f.emb_repo, f.make_service(), log=f.log)
    stop = threading.Event()

    def _set_after_two_iterations() -> None:
        # Wait until at least 2 progress lines appear, then set event.
        import time
        for _ in range(50):
            if f.log.getvalue().count("processed") >= 2:
                stop.set()
                return
            time.sleep(0.01)

    threading.Thread(target=_set_after_two_iterations, daemon=True).start()
    worker.run(interval=0.01, stop_event=stop)

    # At least one iteration ran (progress logged).
    assert "processed" in f.log.getvalue()
