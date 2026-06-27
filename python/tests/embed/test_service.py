"""Tests for EmbedService — embeddings + status rows + any_embedding flag.

Mirrors Go's embed_test.go. Test doubles defined inline:
  - StubVectorStore: records put/search/delete; injectable errors.
  - RecordingEmbeddingRepo: records upsert_status calls.

The FakeContextRepo + CannedFileStore + FakeEmbedder / ErrorEmbedder
from tests/_fakes/ back the rest. StringIO captures warn-and-continue
messages for assertion.

Coverage matrix vs the brief's 6 mandated tests:
  1. happy path                → test_embed_writes_vector_status_done_and_flag
  2. empty after hydration     → test_empty_text_after_hydration_records_failed
  3. embedder failure          → test_embedder_failure_records_failed_no_vector
  4. VectorStore failure       → test_vector_store_failure_records_failed_no_flag_flip
  5. flag-write failure        → test_any_embedding_flag_write_failure_logs_warning_no_raise
  6. model_slug from embedder  → test_model_slug_derived_from_embedder_not_parameter

Plus supplementary coverage for paths the brief left implicit:
  - hydration from item.content (inline case)
  - hydration from FileStore (externalized case)
  - hydration when no content + no URI (title-only embed)
  - hydrate failure (item not in repo)
  - embedder wrong vector count
  - status row write failure logs warning without masking success
  - load-item failure for flag flip (also non-fatal per §3.6)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import StringIO

import pytest

from tests._fakes.canned_filestore import CannedFileStore
from tests._fakes.fake_embedder import ErrorEmbedder, FakeEmbedder
from tests._fakes.fake_repo import FakeContextRepo
from unictx.embed.embedder import ModelInfo
from unictx.embed.service import EmbedService
from unictx.items.models import ContextItem, Kind, Scope, Source

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StubVectorStore:
    """Controllable VectorStore for tests.

    Attributes:
      put_calls: list of (model, item_id, vector) tuples in call order.
      put_err: when non-None, put() raises this.
      data: (model, item_id) → vector. Tests assert membership to
        verify a vector was written.
    """

    put_calls: list[tuple[str, str, list[float]]] = field(default_factory=list)
    put_err: Exception | None = None
    data: dict[tuple[str, str], list[float]] = field(default_factory=dict)

    def put(self, model: str, item_id: str, vector: list[float]) -> None:
        self.put_calls.append((model, item_id, list(vector)))
        if self.put_err is not None:
            raise self.put_err
        self.data[(model, item_id)] = list(vector)

    def search(self, q):  # unused by EmbedService; stub for Protocol satisfaction
        raise NotImplementedError

    def delete(self, model: str, item_id: str) -> None:
        self.data.pop((model, item_id), None)


@dataclass(slots=True)
class RecordingEmbeddingRepo:
    """Records upsert_status calls for EmbedService assertions.

    Attributes:
      calls: list of (item_id, model_slug, status, err_str) tuples
        in call order. Tests assert on this to verify the status row
        policy (every attempt writes a row).
      err: when non-None, upsert_status raises this. Used to exercise
        the log-on-status-write-failure path (warn + don't mask).
    """

    calls: list[tuple[str, str, str, str]] = field(default_factory=list)
    err: Exception | None = None

    def upsert_status(
        self, item_id: str, model_slug: str, status: str, err_str: str
    ) -> None:
        self.calls.append((item_id, model_slug, status, err_str))
        if self.err is not None:
            raise self.err

    # Unused by EmbedService; declared for Protocol structural match.
    def get_status(self, item_id: str, model_slug: str):
        raise NotImplementedError

    def list_failed(self, limit: int):
        raise NotImplementedError

    def list_for_item(self, item_id: str):
        raise NotImplementedError


@dataclass(slots=True)
class UpdateFailingRepo:
    """FakeContextRepo-shaped stub whose update() always raises.

    Used to exercise the §3.6 deviation: flag-write failure is
    non-fatal. Wraps a FakeContextRepo so create/get/etc. work
    normally — only update() fails.

    get_calls is recorded so tests can verify the flag-flip attempt
    was made (proves the success path reached the flag step).
    """

    inner: FakeContextRepo = field(default_factory=FakeContextRepo)
    update_err: Exception = field(
        default_factory=lambda: RuntimeError("simulated DB outage on update")
    )
    get_calls: list[str] = field(default_factory=list)

    def create(self, item):
        return self.inner.create(item)

    def get(self, id):
        self.get_calls.append(id)
        return self.inner.get(id)

    def update(self, item):
        raise self.update_err

    def delete(self, id):
        return self.inner.delete(id)

    def list(self, filter):
        return self.inner.list(filter)

    def next_cursor(self, item):
        return self.inner.next_cursor(item)

    def reindex_fts(self, id, title, summary, content):
        return self.inner.reindex_fts(id, title, summary, content)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Fixture:
    repo: FakeContextRepo
    fs: CannedFileStore
    emb: FakeEmbedder
    vs: StubVectorStore
    emb_repo: RecordingEmbeddingRepo
    log: StringIO
    svc: EmbedService


def _make_fixture(
    *,
    dimension: int = 8,
    model_slug: str = "fake-model",
    emb: FakeEmbedder | None = None,
    repo: FakeContextRepo | None = None,
    vs: StubVectorStore | None = None,
    emb_repo: RecordingEmbeddingRepo | None = None,
) -> _Fixture:
    """Build a wired EmbedService fixture.

    Defaults match Go's newEmbedFixture: dim=8, slug='fake-model',
    FakeEmbedder + FakeContextRepo + CannedFileStore + clean stubs.
    """
    if emb is None:
        emb = FakeEmbedder(
            dimension=dimension,
            model_info=ModelInfo(slug=model_slug, dimension=dimension),
        )
    if repo is None:
        repo = FakeContextRepo()
    if vs is None:
        vs = StubVectorStore()
    if emb_repo is None:
        emb_repo = RecordingEmbeddingRepo()
    log = StringIO()
    svc = EmbedService(emb, vs, repo, CannedFileStore(), emb_repo, log=log)
    return _Fixture(
        repo=repo, fs=CannedFileStore(), emb=emb, vs=vs, emb_repo=emb_repo,
        log=log, svc=svc,
    )


def _make_item(
    *,
    item_id: str = "i-1",
    title: str = "t",
    content: str = "",
    content_uri: str = "",
) -> ContextItem:
    """Build a minimal persisted ContextItem."""
    item = new_context_item_for_test(item_id, title=title, content=content)
    item.content_uri = content_uri
    return item


def new_context_item_for_test(
    item_id: str, *, title: str = "", content: str = ""
) -> ContextItem:
    """Construct a ContextItem with a forced id (tests need deterministic ids).

    new_context_item generates a uuid7; tests want stable ids. We
    construct via new_context_item then overwrite .id (mirrors Go's
    test pattern of assigning item.ID after construction).
    """
    item = ContextItem(
        id=item_id,
        scope=Scope.USER,
        kind=Kind.NOTE,
        source=Source.MANUAL,
        owner_user_id="u-1",
        title=title,
        content=content,
    )
    return item


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_embed_writes_vector_status_done_and_flag() -> None:
    """Happy path: vector + status='done' + any_embedding=1.

    Verifies the four success side-effects:
      1. Embedder.embed called with title + "\n\n" + content.
      2. VectorStore.put called with (model, item_id, vector).
      3. EmbeddingRepo.upsert_status called with status='done'.
      4. repo.update called with item.any_embedding=1.
    """
    f = _make_fixture()
    item = _make_item(item_id="i-1", title="deploy guide", content="how to deploy")
    f.repo.create(item)

    f.svc.embed_item("i-1", "deploy guide", "how to deploy")

    # Embedder received the composed text.
    assert f.emb.embed_calls == [["deploy guide\n\nhow to deploy"]]

    # VectorStore.put received the model slug derived from the embedder.
    assert len(f.vs.put_calls) == 1
    model, item_id, vector = f.vs.put_calls[0]
    assert model == "fake-model"
    assert item_id == "i-1"
    assert len(vector) == 8

    # Status row written with status='done', no error.
    assert f.emb_repo.calls == [("i-1", "fake-model", "done", "")]

    # any_embedding flag flipped to 1 on the persisted item.
    assert f.repo.get("i-1").any_embedding == 1


# ---------------------------------------------------------------------------
# Empty text after hydration
# ---------------------------------------------------------------------------


def test_empty_text_after_hydration_records_failed() -> None:
    """Title="" + content="" (after hydration) → status='failed', no vector.

    No item.content, no item.content_uri, no title — hydration returns
    "" so text composition strips to empty. This is the only path
    where embed_item raises WITHOUT calling the embedder.
    """
    f = _make_fixture()
    item = _make_item(item_id="i-1", title="", content="")
    f.repo.create(item)

    with pytest.raises(RuntimeError, match=r"empty text for item i-1"):
        f.svc.embed_item("i-1", "", "")

    # Status='failed' with the empty-text error message.
    assert len(f.emb_repo.calls) == 1
    _, _, status, err_str = f.emb_repo.calls[0]
    assert status == "failed"
    assert "empty text" in err_str
    # No vector written.
    assert f.vs.put_calls == []
    # No embedder call.
    assert f.emb.embed_calls == []


# ---------------------------------------------------------------------------
# Embedder failure
# ---------------------------------------------------------------------------


def test_embedder_failure_records_failed_no_vector() -> None:
    """Embedder raises EmbeddingFailed → status='failed', no vector, raises.

    ErrorEmbedder wraps the underlying reason into EmbeddingFailed.
    EmbedService records status='failed' with the reason text, then
    re-raises (wrapped with "embed item <id>:" prefix).
    """
    f = _make_fixture(
        emb=ErrorEmbedder(
            inner=FakeEmbedder(
                dimension=8,
                model_info=ModelInfo(slug="fake-model", dimension=8),
            ),
            reason="ollama unreachable",
        )
    )
    item = _make_item(item_id="i-1", title="t", content="body")
    f.repo.create(item)

    with pytest.raises(RuntimeError, match=r"embed item i-1"):
        f.svc.embed_item("i-1", "t", "body")

    # Status='failed' with the embedder's reason text (not the wrapped version).
    assert len(f.emb_repo.calls) == 1
    _, _, status, err_str = f.emb_repo.calls[0]
    assert status == "failed"
    assert "ollama unreachable" in err_str
    # No vector written.
    assert f.vs.put_calls == []
    # No flag flip attempted.
    assert f.repo.get("i-1").any_embedding == 0


# ---------------------------------------------------------------------------
# VectorStore failure
# ---------------------------------------------------------------------------


def test_vector_store_failure_records_failed_no_flag_flip() -> None:
    """VectorStore.put raises → status='failed', no flag flip, raises.

    Symmetric with embedder failure: vector write is a hard requirement;
    failure here means the item is NOT vector-searchable. Flag flip
    is not attempted (no point flipping a flag for an item with no vector).
    """
    f = _make_fixture(
        vs=StubVectorStore(put_err=RuntimeError("vec0 virtual table missing")),
    )
    item = _make_item(item_id="i-1", title="t", content="body")
    f.repo.create(item)

    with pytest.raises(RuntimeError, match=r"store vector for i-1"):
        f.svc.embed_item("i-1", "t", "body")

    # put was attempted (and raised).
    assert len(f.vs.put_calls) == 1
    # Status='failed' with the store-vector error text.
    assert len(f.emb_repo.calls) == 1
    _, _, status, err_str = f.emb_repo.calls[0]
    assert status == "failed"
    assert "vec0 virtual table missing" in err_str
    # No flag flip — item still has any_embedding=0.
    assert f.repo.get("i-1").any_embedding == 0


# ---------------------------------------------------------------------------
# any_embedding flag-write failure (§3.6 deviation)
# ---------------------------------------------------------------------------


def test_any_embedding_flag_write_failure_logs_warning_no_raise() -> None:
    """Flag-write (update) failure → status='done', warning logged, no raise.

    DEVIATION from Go (Plan §3.6): Go returns the wrapped update error;
    the Python brief overrides — flag-write is non-fatal. Vec row IS
    the source of truth for "embedded"; the flag is a perf optimization.
    Status stays 'done' (the embed succeeded), warning surfaces the
    observability gap, no exception raised.
    """
    inner = FakeContextRepo()
    item = _make_item(item_id="i-1", title="t", content="body")
    inner.create(item)
    repo = UpdateFailingRepo(inner=inner)
    f = _make_fixture(repo=repo)

    # No raise — deviation from Go.
    f.svc.embed_item("i-1", "t", "body")

    # Vector IS written (success path reached put).
    assert len(f.vs.put_calls) == 1
    # Flag flip was attempted (get called, update raised).
    assert "i-1" in repo.get_calls
    # Status='done' — embed succeeded; flag-write failure is non-fatal.
    assert f.emb_repo.calls == [("i-1", "fake-model", "done", "")] or (
        len(f.emb_repo.calls) == 1 and f.emb_repo.calls[0][2] == "done"
    )
    # Warning logged so operators see the gap.
    assert "any_embedding" in f.log.getvalue()


def test_any_embedding_flag_load_failure_logs_warning_no_raise() -> None:
    """Flag-load (repo.get) failure → status='done', warning logged, no raise.

    The §3.6 deviation treats both load-item and update-item failures
    as non-fatal: vec row IS source of truth; flag is perf only.
    A missing item (repo.get raises ItemNotFound) shouldn't fail the
    embed — the vec row is already written and searchable.
    """
    # Item NOT in repo — repo.get will raise ItemNotFound.
    f = _make_fixture()

    # No raise — deviation from Go (which returns wrapped Get error).
    f.svc.embed_item("ghost-id", "t", "body")

    # Vector IS written.
    assert len(f.vs.put_calls) == 1
    assert f.vs.put_calls[0][1] == "ghost-id"
    # Status='done'.
    assert len(f.emb_repo.calls) == 1
    assert f.emb_repo.calls[0][2] == "done"
    # Warning logged.
    assert "any_embedding" in f.log.getvalue() or "load item" in f.log.getvalue()


# ---------------------------------------------------------------------------
# model_slug from embedder (not parameter)
# ---------------------------------------------------------------------------


def test_model_slug_derived_from_embedder_not_parameter() -> None:
    """model_slug comes from embedder.model().slug — never a parameter.

    Verifies the brief mandate: Go's Embed signature has no model_slug
    field; it's derived internally from embedder.Model().Slug. The
    vector + status rows use whatever the embedder reports.
    """
    custom = ModelInfo(slug="custom-slug", dimension=4)
    f = _make_fixture(
        emb=FakeEmbedder(dimension=4, model_info=custom),
    )
    item = _make_item(item_id="i-1", title="t", content="body")
    f.repo.create(item)

    f.svc.embed_item("i-1", "t", "body")

    # Vector + status rows carry the embedder's slug, not a parameter.
    assert f.vs.put_calls[0][0] == "custom-slug"
    assert f.emb_repo.calls[0][1] == "custom-slug"


# ---------------------------------------------------------------------------
# Hydration paths
# ---------------------------------------------------------------------------


def test_hydrates_from_inline_content_when_content_empty() -> None:
    """Caller passes content="" → service reads item.content from repo.

    This is the inline-content hydration case: item.content is set
    (small notes), caller doesn't bother passing content again.
    """
    f = _make_fixture()
    item = _make_item(item_id="i-1", title="t", content="inline body")
    f.repo.create(item)

    f.svc.embed_item("i-1", "t", "")  # empty content → hydrate

    # Embedder received the hydrated inline content.
    assert f.emb.embed_calls == [["t\n\ninline body"]]


def test_hydrates_from_filestore_when_content_empty() -> None:
    """Externalized item: caller passes content="" → service hydrates from fs.

    Plan 2b fix: items with content > CONTENT_INLINE_LIMIT have their
    content_uri set + content cleared after fs.put. EmbedService must
    hydrate from FileStore via content_uri, else the embed is title-only.
    """
    f = _make_fixture()
    item = _make_item(item_id="i-1", title="externalized", content="")
    # Seed the FileStore-backed dict (CannedFileStore returns file://<sha>).
    content_bytes = b"this content lives in the filestore not inline"
    uri, _ = f.svc._fs.put(content_bytes, "text/plain")
    item.content_uri = uri
    f.repo.create(item)

    f.svc.embed_item("i-1", "externalized", "")

    # Embedder received the hydrated externalized content.
    assert len(f.emb.embed_calls) == 1
    text = f.emb.embed_calls[0][0]
    assert "externalized" in text, "title in embed text"
    assert "this content lives in the filestore" in text, (
        "hydrated content in embed text (would fail pre-2b)"
    )


def test_hydrate_returns_empty_when_no_content_no_uri() -> None:
    """Item with no content AND no content_uri → hydration returns "".

    Combined with a non-empty title this is the legit title-only embed
    case (e.g., a one-line note with only a title). Combined with an
    empty title it triggers the empty-text failure path.
    """
    f = _make_fixture()
    item = _make_item(item_id="i-1", title="title only", content="")
    f.repo.create(item)

    f.svc.embed_item("i-1", "title only", "")

    # Title-only embed succeeded.
    assert f.emb.embed_calls == [["title only"]]
    assert len(f.vs.put_calls) == 1
    assert f.emb_repo.calls[0][2] == "done"


def test_hydrate_failure_records_failed_status() -> None:
    """Hydration error (item not in repo) → status='failed', raises.

    The empty-text case also starts with missing-item hydration, but
    here we want to verify the hydrate-exception path explicitly:
    repo.get raises ItemNotFound → EmbedService records 'failed' with
    the original error text (not the wrapped version).
    """
    f = _make_fixture()
    # Item NOT in repo; content="" triggers hydration → ItemNotFound.

    with pytest.raises(RuntimeError, match=r"hydrate content for ghost"):
        f.svc.embed_item("ghost", "t", "")

    # Status='failed' with the original hydrate error text.
    assert len(f.emb_repo.calls) == 1
    _, _, status, err_str = f.emb_repo.calls[0]
    assert status == "failed"
    assert "ghost" in err_str
    # No vector, no embedder call.
    assert f.vs.put_calls == []
    assert f.emb.embed_calls == []


# ---------------------------------------------------------------------------
# Embedder wrong vector count
# ---------------------------------------------------------------------------


def test_embedder_wrong_vector_count_records_failed() -> None:
    """Embedder returns !=1 vectors → status='failed', no vector, raises.

    Embedder.embed is contractually bound to return one vector per
    input text. A malformed backend that returns the wrong count
    would corrupt the vec0 row — refuse and record failure.
    """

    @dataclass(slots=True)
    class _TwoVectorEmbedder:
        """Returns 2 vectors for 1 input to exercise the count check."""

        def model(self) -> ModelInfo:
            return ModelInfo(slug="fake-model", dimension=8)

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 8, [0.2] * 8]

    f = _make_fixture(emb=_TwoVectorEmbedder())  # type: ignore[arg-type]
    item = _make_item(item_id="i-1", title="t", content="body")
    f.repo.create(item)

    with pytest.raises(RuntimeError, match=r"2 vectors, expected 1"):
        f.svc.embed_item("i-1", "t", "body")

    # Status='failed'.
    assert f.emb_repo.calls[0][2] == "failed"
    # No vector written.
    assert f.vs.put_calls == []


# ---------------------------------------------------------------------------
# Status-write failure logs warning without masking success
# ---------------------------------------------------------------------------


def test_status_write_failure_logs_warning_does_not_mask_success() -> None:
    """EmbeddingRepo.upsert_status raises → warn, but embed still succeeds.

    Status-row write is best-effort: failure here must NEVER mask the
    original embed result. If the embed succeeds (vector + flag written)
    but status-write fails, EmbedService logs a warning and returns None.
    """
    f = _make_fixture(
        emb_repo=RecordingEmbeddingRepo(err=RuntimeError("emb_repo DB locked")),
    )
    item = _make_item(item_id="i-1", title="t", content="body")
    f.repo.create(item)

    # No raise — status-write failure is best-effort.
    f.svc.embed_item("i-1", "t", "body")

    # Vector + flag still written.
    assert len(f.vs.put_calls) == 1
    assert f.repo.get("i-1").any_embedding == 1
    # Warning logged.
    assert "failed to record embedding status" in f.log.getvalue()
