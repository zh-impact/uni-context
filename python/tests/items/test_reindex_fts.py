"""Tests for ReindexFTSService — bulk-rewrite context_fts rows.

Mirrors Go's reindex_fts_test.go. The service walks items, hydrates
externalized content from FileStore, calls repo.reindex_fts. Tests
exercise: bulk reindex, dry run, per-item failure continuation,
inline-items skipped, limit honored, stop_event honored.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from io import StringIO

from tests._fakes.canned_filestore import CannedFileStore
from tests._fakes.fake_repo import FakeContextRepo
from unictx.items.models import ContextItem, Kind, Scope, Source
from unictx.items.reindex_fts import ReindexFTSService

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FailingFileStore:
    """CannedFileStore-shaped stub whose get() always raises for a target URI.

    Used to exercise the hydrate-failure path. Wraps a CannedFileStore so
    puts/deletes work normally; only get() on the target URI raises.
    """

    inner: CannedFileStore = field(default_factory=CannedFileStore)
    fail_uri: str = ""

    def put(self, content, mime):
        return self.inner.put(content, mime)

    def get(self, uri):
        if uri == self.fail_uri:
            raise RuntimeError("fs: simulated read error")
        return self.inner.get(uri)

    def delete(self, uri):
        return self.inner.delete(uri)


class _FlakyReindexRepo(FakeContextRepo):
    """FakeContextRepo subclass whose reindex_fts raises for one item.

    FakeContextRepo is ``@dataclass(slots=True)`` — we don't add fields
    here, just override one method, so subclassing works. Used to
    exercise the reindex_fts-error-continue path.
    """

    def __init__(self, fail_id: str, err: Exception) -> None:  # type: ignore[no-redef]
        # Skip dataclass __init__; manually init the slots we need.
        # FakeContextRepo's slots are: items, reindex_fts_calls,
        # reindex_fts_args, create_err.
        object.__setattr__(self, "items", {})
        object.__setattr__(self, "reindex_fts_calls", 0)
        object.__setattr__(self, "reindex_fts_args", [])
        object.__setattr__(self, "create_err", None)
        self._fail_id = fail_id
        self._err = err

    def reindex_fts(self, id, title, summary, content):  # type: ignore[override]
        if id == self._fail_id:
            raise self._err
        # Mirror FakeContextRepo's recording semantics for the success case.
        object.__setattr__(self, "reindex_fts_calls", self.reindex_fts_calls + 1)
        self.reindex_fts_args.append((id, title, summary, content))


class _PagingRepo(FakeContextRepo):
    """FakeContextRepo subclass that returns a fake cursor on first list().

    Used to exercise the multi-page progress-logging path. The first
    list() call returns the items + a non-empty next_cursor; subsequent
    calls return ([], "") so the loop exits.
    """

    def __init__(self) -> None:  # type: ignore[no-redef]
        object.__setattr__(self, "items", {})
        object.__setattr__(self, "reindex_fts_calls", 0)
        object.__setattr__(self, "reindex_fts_args", [])
        object.__setattr__(self, "create_err", None)
        self._call_count = 0

    def list(self, filter):  # type: ignore[override]
        self._call_count += 1
        items = list(self.items.values())
        if self._call_count == 1:
            return items, "fake-cursor"
        return [], ""


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _make_item(
    item_id: str,
    *,
    title: str = "",
    content: str = "",
    content_uri: str = "",
) -> ContextItem:
    """Build a minimal ContextItem for reindex-fts tests."""
    return ContextItem(
        id=item_id,
        scope=Scope.USER, kind=Kind.NOTE, source=Source.MANUAL,
        owner_user_id="u-1",
        title=title,
        content=content,
        content_uri=content_uri,
    )


@dataclass(slots=True)
class _Fixture:
    repo: FakeContextRepo
    fs: CannedFileStore
    log: StringIO
    svc: ReindexFTSService


def _make_fixture(fs: CannedFileStore | None = None) -> _Fixture:
    repo = FakeContextRepo()
    fs = fs if fs is not None else CannedFileStore()
    log = StringIO()
    svc = ReindexFTSService(repo, fs, log=log)
    return _Fixture(repo=repo, fs=fs, log=log, svc=svc)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reindexes_externalized_items() -> None:
    """Externalized items (content_uri set, content empty) → FTS rewritten."""
    f = _make_fixture()
    # Seed the FileStore so hydrate succeeds.
    content_a = b"alpha body text"
    content_b = b"beta body text"
    uri_a, _ = f.fs.put(content_a, "text/plain")
    uri_b, _ = f.fs.put(content_b, "text/plain")
    f.repo.create(_make_item("a", title="A", content_uri=uri_a))
    f.repo.create(_make_item("b", title="B", content_uri=uri_b))

    report = f.svc.run()

    assert report.scanned == 2
    assert report.reindexed == 2
    assert report.failed == 0
    assert f.repo.reindex_fts_calls == 2
    # Args recorded for assertion.
    assert ("a", "A", "", "alpha body text") in f.repo.reindex_fts_args
    assert ("b", "B", "", "beta body text") in f.repo.reindex_fts_args


def test_inline_items_skipped() -> None:
    """Items with content inline (no content_uri OR content set) skipped."""
    f = _make_fixture()
    # Pure inline: content_uri="" → skip.
    f.repo.create(_make_item("inline-1", content="inline body"))
    # Mixed: content_uri set AND content non-empty (shouldn't happen in
    # practice, but the guard treats it as inline to avoid double-write).
    f.repo.create(_make_item("inline-2", content="inline", content_uri="file://x"))

    report = f.svc.run()

    assert report.scanned == 0
    assert f.repo.reindex_fts_calls == 0


def test_dry_run_does_not_reindex() -> None:
    """dry_run=True → increment scanned only; no fs.get, no reindex_fts."""
    f = _make_fixture()
    uri, _ = f.fs.put(b"body", "text/plain")
    f.repo.create(_make_item("a", title="A", content_uri=uri))

    report = f.svc.run(dry_run=True)

    assert report.scanned == 1
    assert report.reindexed == 0
    assert f.repo.reindex_fts_calls == 0


def test_hydrate_failure_continues() -> None:
    """fs.get error → record failure, run continues."""
    fs = _FailingFileStore(fail_uri="file://broken")
    # Seed one good item + one broken.
    good_uri, _ = fs.put(b"good body", "text/plain")
    f = _make_fixture(fs=fs)
    f.repo.create(_make_item("good", title="G", content_uri=good_uri))
    f.repo.create(_make_item("bad", title="B", content_uri="file://broken"))

    report = f.svc.run()

    assert report.scanned == 2
    assert report.reindexed == 1, "good item reindexed"
    assert report.failed == 1
    assert len(report.failures) == 1
    assert report.failures[0].item_id == "bad"
    assert "hydrate" in report.failures[0].error


def test_reindex_fts_failure_continues() -> None:
    """repo.reindex_fts error → record failure, run continues."""
    repo = _FlakyReindexRepo(fail_id="bad", err=RuntimeError("fts5 corruption"))
    fs = CannedFileStore()
    uri_good, _ = fs.put(b"good", "text/plain")
    uri_bad, _ = fs.put(b"bad", "text/plain")
    repo.create(_make_item("good", title="G", content_uri=uri_good))
    repo.create(_make_item("bad", title="B", content_uri=uri_bad))

    svc = ReindexFTSService(repo, fs, log=StringIO())
    report = svc.run()

    assert report.scanned == 2
    assert report.reindexed == 1
    assert report.failed == 1
    assert report.failures[0].item_id == "bad"
    assert "fts5 corruption" in report.failures[0].error


def test_limit_honored() -> None:
    """limit caps the candidate count."""
    f = _make_fixture()
    for i in range(5):
        uri, _ = f.fs.put(f"body {i}".encode(), "text/plain")
        f.repo.create(_make_item(f"id-{i}", title=f"title-{i}", content_uri=uri))

    report = f.svc.run(limit=3)
    assert report.scanned == 3
    assert report.reindexed == 3


def test_stop_event_returns_partial_report() -> None:
    """stop_event pre-set → no items scanned, empty report returned."""
    f = _make_fixture()
    uri, _ = f.fs.put(b"body", "text/plain")
    f.repo.create(_make_item("a", content_uri=uri))

    stop = threading.Event()
    stop.set()
    report = f.svc.run(stop_event=stop)
    assert report.scanned == 0
    assert f.repo.reindex_fts_calls == 0


def test_progress_logged_per_page() -> None:
    """Per-page progress line written to log.

    With page size 200 and a small item count, the progress line fires
    only when pagination actually advances (next_cursor != ""). The
    FakeContextRepo returns "" for next_cursor on every call, so the
    log message is suppressed on a single-page run.
    """
    f = _make_fixture()
    uri, _ = f.fs.put(b"body", "text/plain")
    f.repo.create(_make_item("a", content_uri=uri))

    f.svc.run()

    # Single-page run: no per-page progress (next_cursor=="" → break).
    assert "reindex-fts" not in f.log.getvalue()


def test_progress_logged_when_pagination_advances() -> None:
    """Multi-page run: progress line written after each non-final page."""
    repo = _PagingRepo()
    fs = CannedFileStore()
    uri, _ = fs.put(b"body", "text/plain")
    repo.create(_make_item("a", content_uri=uri))

    log = StringIO()
    svc = ReindexFTSService(repo, fs, log=log)
    svc.run()
    assert "reindex-fts: 1 items scanned" in log.getvalue()
