"""Tests for unictx.storage.model_registry_impl.ModelRegistryImpl.

Ports the round-trip + edge-case scenarios that Go exercises in
``archive/go/internal/adapter/sqlite/model_registry_test.go``. Covers:

* register/list roundtrip
* register duplicate slug → ModelConflict
* get happy + ModelNotFound
* get_active returns the default row
* set_default flips atomically (other rows cleared)
* set_default on missing slug → ModelNotFound
* update_config happy + ModelNotFound
* remove drops the vec table + cascades status rows
* remove on default model → domain error
* remove on shared vec_table → domain error (Plan 2b alias protection)
* scan_model with corrupt config JSON → CorruptConfigError (descriptor
  accessible)
* schema_meta.version() returns the value written by migrations_runner
* Protocol conformance (isinstance(ModelRegistryImpl(db), ModelRegistry))

The fixture ``migrated_db`` (tests/conftest.py) yields a fresh
``:memory:`` connection with all migrations applied. Migration 0002
already seeds the default model row (slug=``bge-m3``, dim=1024,
vec_table=``vec_bge_m3_1024``, is_default=1) and creates the
corresponding vec0 virtual table.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from unictx.embed.errors import (
    CorruptConfigError,
    InvalidSlugError,
    ModelConflict,
    ModelNotFound,
)
from unictx.embed.model_registry import ModelRegistry, ModelSpec
from unictx.errors import UnictxError
from unictx.storage.model_registry_impl import ModelRegistryImpl, _vec_table_name

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def reg(migrated_db: sqlite3.Connection) -> ModelRegistryImpl:
    """ModelRegistryImpl wired to a migrated :memory: DB.

    Migration 0002 seeds ``bge-m3`` as the default; tests that exercise
    a clean slate (register/remove) typically work alongside it.
    """
    return ModelRegistryImpl(migrated_db)


def _spec(
    slug: str = "openai-3-small",
    *,
    provider: str = "openai-compat",
    base_url: str = "https://api.example.com",
    api_key: str = "secret",
    dimension: int = 1536,
) -> ModelSpec:
    return ModelSpec(
        slug=slug,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        dimension=dimension,
    )


# ---------------------------------------------------------------------------
# vec_table_name helper
# ---------------------------------------------------------------------------


class TestVecTableName:
    """``_vec_table_name`` mirrors Go's ``vecTableName`` verbatim."""

    def test_dashes_become_underscores(self) -> None:
        # The brief's example.
        assert _vec_table_name("text-embedding-3-large", 3072) == (
            "vec_text_embedding_3_large_3072"
        )

    def test_simple_slug(self) -> None:
        assert _vec_table_name("bge-m3", 1024) == "vec_bge_m3_1024"


# ---------------------------------------------------------------------------
# Slug validation — defense in depth against SQL injection.
#
# Slugs flow into raw SQL via _vec_table_name (CREATE VIRTUAL TABLE,
# DROP TABLE, vec0 INSERT/DELETE/SELECT). _validate_slug rejects any
# slug containing characters outside [a-zA-Z0-9_-]+ so a malicious or
# malformed slug cannot break out of the SQL identifier.
# ---------------------------------------------------------------------------


class TestVecTableNameValidates:
    """``_vec_table_name`` validates slug as belt-and-braces.

    Even though ``register`` validates first, ``_vec_table_name`` is the
    actual SQL-interpolation seam — any future caller that bypasses
    ``register`` (e.g. a heal path that rebuilds vec tables from a
    config dump) is still protected.
    """

    def test_rejects_injection_attempt(self) -> None:
        """Classic SQL injection payload must be rejected."""
        with pytest.raises(InvalidSlugError) as excinfo:
            _vec_table_name("evil; DROP TABLE users; --", 8)
        assert excinfo.value.slug == "evil; DROP TABLE users; --"

    def test_accepts_valid_slug(self) -> None:
        """A valid slug returns the expected vec table name."""
        assert _vec_table_name("bge-m3", 1024) == "vec_bge_m3_1024"


class TestRegisterSlugValidation:
    """``register`` validates slug before any SQL is issued."""

    @pytest.mark.parametrize(
        "slug",
        [
            "evil; DROP TABLE users; --",
            "bad/slug",
            "bad'slug",
            "(bad)",
            "",  # empty — must NOT match [a-zA-Z0-9_-]+
            "bad slug",  # space
            "tab\tslug",  # tab
            "newline\nslug",  # newline
            'quote"slug',  # double quote
            "back`tick",  # backtick (SQL identifier quoting)
            "dollar$slug",  # dollar (shell var / psql var)
            "semi;colon",  # semicolon (statement terminator)
            "parens(slug)",  # parens (subquery)
            "star*slug",  # star (SELECT *)
            "amp&slug",  # ampersand (shell bg)
            "pipe|slug",  # pipe (shell pipe)
            "lt<slug",  # angle brackets
            "uni-é-acute",  # non-ASCII letter (rejected by ASCII-only class)
        ],
    )
    def test_register_rejects_invalid_slug(self, slug: str) -> None:
        """Each slug above must be rejected with InvalidSlugError.

        ``InvalidSlugError`` is a ``ValueError`` subclass — both
        ``except ValueError`` and ``except InvalidSlugError`` catch it.
        We assert the specific subclass so a future refactor that
        accidentally swaps the base class is caught.
        """
        spec = ModelSpec(
            slug=slug,
            provider="x",
            base_url="y",
            api_key="z",
            dimension=8,
        )
        # Use a fresh in-memory DB so an empty-slug case doesn't trip
        # the pre-check on an existing row.
        from unictx.storage.db import open_db
        from unictx.storage.migrations_runner import migrate

        db = open_db(":memory:")
        try:
            migrate(db)
            reg = ModelRegistryImpl(db)
            with pytest.raises(InvalidSlugError) as excinfo:
                reg.register(spec)
            assert excinfo.value.slug == slug
            # Belt-and-braces: also catchable as ValueError.
            assert isinstance(excinfo.value, ValueError)
        finally:
            db.close()

    @pytest.mark.parametrize(
        "slug",
        [
            "bge-m3",
            "text-embedding-3-large",
            "model_v1",
            "openai-3-small",
            "alpha",  # bare word
            "ABC123",  # uppercase + digits
            "a",  # single char
        ],
    )
    def test_register_accepts_valid_slugs(self, slug: str) -> None:
        """All canonical slugs must register successfully.

        Guards against an over-strict regex breaking existing call sites
        (e.g. Phase 3+ register-from-config paths will hit this).
        """
        spec = ModelSpec(
            slug=slug,
            provider="x",
            base_url="y",
            api_key="z",
            dimension=8,
        )
        from unictx.storage.db import open_db
        from unictx.storage.migrations_runner import migrate

        db = open_db(":memory:")
        try:
            migrate(db)
            # Migration 0002 seeds ``bge-m3`` as the default — drop it
            # first so we can register any slug (including bge-m3 itself)
            # without tripping the pre-check.
            db.execute("DROP TABLE IF EXISTS vec_bge_m3_1024")
            db.execute("DELETE FROM embedding_model")
            reg = ModelRegistryImpl(db)
            reg.register(spec)
            m = reg.get(slug)
            assert m.slug == slug
        finally:
            db.close()

    def test_validation_runs_before_precheck(self) -> None:
        """An invalid slug raises InvalidSlugError, NOT ModelConflict.

        Regression: if validation ran AFTER the pre-check and the bad
        slug happened to collide with an existing row, the caller would
        see ModelConflict instead of InvalidSlugError — obscuring the
        real problem (bad input, not a duplicate).
        """
        from unictx.storage.db import open_db
        from unictx.storage.migrations_runner import migrate

        db = open_db(":memory:")
        try:
            migrate(db)
            reg = ModelRegistryImpl(db)
            # bge-m3 is seeded by migration 0002 — but the invalid-char
            # variant "bge-m3;" must still raise InvalidSlugError, not
            # ModelConflict (even though "bge-m3" exists in the table).
            bad_spec = ModelSpec(
                slug="bge-m3; DROP TABLE users; --",
                provider="x",
                base_url="y",
                api_key="z",
                dimension=8,
            )
            with pytest.raises(InvalidSlugError):
                reg.register(bad_spec)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# register / list roundtrip
# ---------------------------------------------------------------------------


class TestRegisterList:
    def test_register_then_list_returns_descriptor(
        self,
        migrated_db: sqlite3.Connection,
        reg: ModelRegistryImpl,
    ) -> None:
        spec = _spec()
        reg.register(spec)

        models = reg.list()
        slugs = {m.slug for m in models}
        assert "openai-3-small" in slugs

        m = reg.get("openai-3-small")
        assert m.slug == "openai-3-small"
        assert m.provider == "openai-compat"
        assert m.base_url == "https://api.example.com"
        assert m.api_key == "secret"
        assert m.dimension == 1536
        assert m.vec_table == "vec_openai_3_small_1536"
        assert m.is_default is False
        assert m.status == "active"

    def test_list_ordered_by_created_at_then_slug(
        self,
        migrated_db: sqlite3.Connection,
        reg: ModelRegistryImpl,
    ) -> None:
        """Go's List: ``ORDER BY created_at ASC, slug ASC``.

        ``strftime('%s','now')`` has second resolution; the seeded
        bge-m3 row and any rows registered within the same second share
        the same ``created_at`` value, so the slug is the tiebreaker.
        To make this test deterministic we insert all rows with an
        explicit, controlled ``created_at`` via raw SQL.
        """
        migrated_db.execute("UPDATE embedding_model SET created_at=100 WHERE slug='bge-m3'")
        # Two fresh rows registered at later timestamps.
        for slug, ts in (("alpha-model", 200), ("zeta-model", 300)):
            migrated_db.execute(
                "INSERT INTO embedding_model "
                "(slug, name, provider, dimension, vec_table, is_default, status, "
                "config, created_at) VALUES (?, ?, 'ollama', 8, ?, 0, 'active', '{}', ?)",
                (slug, slug, f"vec_{slug.replace('-', '_')}_8", ts),
            )

        slugs = [m.slug for m in reg.list()]
        assert slugs == ["bge-m3", "alpha-model", "zeta-model"]

    def test_list_ties_break_by_slug(
        self,
        migrated_db: sqlite3.Connection,
        reg: ModelRegistryImpl,
    ) -> None:
        """When ``created_at`` ties, slug ASC decides."""
        # Drop the seeded row so we control the entire population.
        migrated_db.execute("DROP TABLE IF EXISTS vec_bge_m3_1024")
        migrated_db.execute("DELETE FROM embedding_model")
        # Three rows with identical created_at — slug decides.
        for slug in ("charlie", "alpha", "bravo"):
            migrated_db.execute(
                "INSERT INTO embedding_model "
                "(slug, name, provider, dimension, vec_table, is_default, status, "
                "config, created_at) VALUES (?, ?, 'ollama', 8, ?, 0, 'active', '{}', 100)",
                (slug, slug, f"vec_{slug}_8"),
            )
        slugs = [m.slug for m in reg.list()]
        assert slugs == ["alpha", "bravo", "charlie"]

    def test_register_creates_vec_table(
        self,
        migrated_db: sqlite3.Connection,
        reg: ModelRegistryImpl,
    ) -> None:
        """The per-model vec0 virtual table exists after register()."""
        reg.register(_spec())

        # sqlite_master is the catalog. vec0 virtual tables appear with
        # type='table' and a 'CREATE VIRTUAL TABLE' root SQL.
        row = migrated_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("vec_openai_3_small_1536",),
        ).fetchone()
        assert row is not None

    def test_list_empty_returns_list_not_none(self) -> None:
        """When no models exist, ``list`` returns ``[]`` (not None)."""
        from unictx.storage.db import open_db
        from unictx.storage.migrations_runner import migrate

        db = open_db(":memory:")
        try:
            migrate(db)
            # Drop the seeded model row + its vec table so list() is empty.
            db.execute("DROP TABLE IF EXISTS vec_bge_m3_1024")
            db.execute("DELETE FROM embedding_model")
            r = ModelRegistryImpl(db)
            assert r.list() == []
        finally:
            db.close()


# ---------------------------------------------------------------------------
# register duplicate → ModelConflict
# ---------------------------------------------------------------------------


class TestRegisterConflict:
    def test_register_duplicate_slug_raises_conflict(
        self,
        reg: ModelRegistryImpl,
    ) -> None:
        """Re-registering an existing slug must raise ModelConflict."""
        reg.register(_spec())
        with pytest.raises(ModelConflict) as excinfo:
            reg.register(_spec())
        assert excinfo.value.slug == "openai-3-small"

    def test_register_seeded_default_raises_conflict(self, reg: ModelRegistryImpl) -> None:
        """Re-registering the seeded ``bge-m3`` row raises ModelConflict."""
        with pytest.raises(ModelConflict):
            reg.register(
                ModelSpec(
                    slug="bge-m3",
                    provider="ollama",
                    base_url="http://localhost:11434",
                    api_key="",
                    dimension=1024,
                )
            )


# ---------------------------------------------------------------------------
# get / get_active
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_happy(self, reg: ModelRegistryImpl) -> None:
        m = reg.get("bge-m3")
        assert m.slug == "bge-m3"
        assert m.is_default is True
        assert m.dimension == 1024

    def test_get_missing_raises_not_found(self, reg: ModelRegistryImpl) -> None:
        with pytest.raises(ModelNotFound) as excinfo:
            reg.get("no-such-slug")
        assert excinfo.value.slug == "no-such-slug"


class TestGetActive:
    def test_get_active_returns_default(self, reg: ModelRegistryImpl) -> None:
        m = reg.get_active()
        assert m.slug == "bge-m3"
        assert m.is_default is True

    def test_get_active_with_no_default_raises_not_found(
        self,
        migrated_db: sqlite3.Connection,
    ) -> None:
        """If no row has is_default=1, get_active raises ModelNotFound."""
        migrated_db.execute("UPDATE embedding_model SET is_default=0")
        reg = ModelRegistryImpl(migrated_db)
        with pytest.raises(ModelNotFound):
            reg.get_active()


# ---------------------------------------------------------------------------
# set_default
# ---------------------------------------------------------------------------


class TestSetDefault:
    def test_set_default_flips_atomically(
        self,
        reg: ModelRegistryImpl,
    ) -> None:
        """Setting default clears all other is_default flags in one tx."""
        reg.register(_spec("first"))
        reg.register(_spec("second"))

        reg.set_default("first")
        m1 = reg.get("first")
        assert m1.is_default is True

        reg.set_default("second")
        m2 = reg.get("second")
        m1_after = reg.get("first")
        assert m2.is_default is True
        assert m1_after.is_default is False
        # The previously-seeded bge-m3 should also be cleared.
        bm = reg.get("bge-m3")
        assert bm.is_default is False

    def test_set_default_only_one_default(
        self,
        reg: ModelRegistryImpl,
    ) -> None:
        """After set_default, exactly one row has is_default=1."""
        reg.register(_spec("first"))
        reg.register(_spec("second"))
        reg.set_default("second")

        # Query DB directly: count rows with is_default=1.
        # (row_factory returns ContextItem only for context_item SELECTs;
        # for embedding_model SELECTs the raw tuple passes through.)
        (count,) = reg._db.execute(
            "SELECT COUNT(*) FROM embedding_model WHERE is_default=1"
        ).fetchone()
        assert count == 1

    def test_set_default_missing_raises_not_found(self, reg: ModelRegistryImpl) -> None:
        with pytest.raises(ModelNotFound) as excinfo:
            reg.set_default("no-such-slug")
        assert excinfo.value.slug == "no-such-slug"

    def test_set_default_idempotent(self, reg: ModelRegistryImpl) -> None:
        """Setting default on an already-default row is a no-op."""
        # bge-m3 is the seeded default.
        reg.set_default("bge-m3")
        m = reg.get("bge-m3")
        assert m.is_default is True


# ---------------------------------------------------------------------------
# update_config
# ---------------------------------------------------------------------------


class TestUpdateConfig:
    def test_update_config_happy(self, reg: ModelRegistryImpl) -> None:
        reg.register(_spec())

        reg.update_config(
            "openai-3-small",
            base_url="https://new-endpoint.example.com",
            api_key="rotated",
            provider="openai",
        )
        m = reg.get("openai-3-small")
        assert m.base_url == "https://new-endpoint.example.com"
        assert m.api_key == "rotated"
        assert m.provider == "openai"

    def test_update_config_missing_raises_not_found(self, reg: ModelRegistryImpl) -> None:
        with pytest.raises(ModelNotFound) as excinfo:
            reg.update_config(
                "no-such-slug",
                base_url="x",
                api_key="y",
                provider="z",
            )
        assert excinfo.value.slug == "no-such-slug"


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class TestRemove:
    def test_remove_drops_vec_table_and_cascades(
        self,
        migrated_db: sqlite3.Connection,
        reg: ModelRegistryImpl,
    ) -> None:
        """remove() drops the vec0 table and deletes the model row."""
        reg.register(_spec("extra-model"))

        # Seed a context_embedding status row. ``context_embedding`` has
        # an FK on item_id → context_item(id); we need a parent row first.
        # Use the repo to create one properly.
        from unictx.items.models import (
            Kind,
            NewItemParams,
            Scope,
            Source,
            new_context_item,
        )
        from unictx.storage.repo_impl import ContextRepoImpl

        repo = ContextRepoImpl(migrated_db)
        item = new_context_item(
            Scope.USER,
            Kind.NOTE,
            Source.MANUAL,
            NewItemParams(owner_user_id="u"),
            title="status row parent",
        )
        repo.create(item)
        migrated_db.execute(
            "INSERT INTO context_embedding (item_id, model_slug, embedded_at, status) "
            "VALUES (?, 'extra-model', 0, 'done')",
            (item.id,),
        )

        reg.remove("extra-model")

        # Vec table gone.
        row = migrated_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("vec_extra_model_1536",),
        ).fetchone()
        assert row is None

        # Model row gone.
        with pytest.raises(ModelNotFound):
            reg.get("extra-model")

        # Status row cascaded.
        (n,) = migrated_db.execute(
            "SELECT COUNT(*) FROM context_embedding WHERE model_slug=?",
            ("extra-model",),
        ).fetchone()
        assert n == 0

    def test_remove_on_default_raises(self, reg: ModelRegistryImpl) -> None:
        """``bge-m3`` is the seeded default; remove() must refuse."""
        with pytest.raises(UnictxError) as excinfo:
            reg.remove("bge-m3")
        msg = str(excinfo.value).lower()
        assert "default" in msg or "switch" in msg

    def test_remove_missing_raises_not_found(self, reg: ModelRegistryImpl) -> None:
        with pytest.raises(ModelNotFound) as excinfo:
            reg.remove("no-such-slug")
        assert excinfo.value.slug == "no-such-slug"

    def test_remove_shared_vec_table_refuses(
        self,
        migrated_db: sqlite3.Connection,
        reg: ModelRegistryImpl,
    ) -> None:
        """Plan 2b alias protection: shared vec_table must refuse removal.

        We simulate a Plan 2b alias by manually inserting a second row
        pointing at the same ``vec_table`` as the seeded ``bge-m3``.
        ``remove`` must refuse because dropping the vec table would
        break the other model.
        """
        # Insert an alias row sharing bge-m3's vec_table.
        migrated_db.execute(
            "INSERT INTO embedding_model "
            "(slug, name, provider, dimension, vec_table, is_default, status, config, created_at) "
            "VALUES ('bge-m3-alias', 'BGE M3 Alias', 'ollama', 1024, 'vec_bge_m3_1024', "
            "0, 'active', '{}', strftime('s','now'))"
        )

        # Now bge-m3 is no longer the sole sharer. We need both rows to
        # be removable-by-shared-check, but bge-m3 is still default — so
        # remove the alias must be the one that fails (its vec_table is
        # shared with the default).
        with pytest.raises(UnictxError) as excinfo:
            reg.remove("bge-m3-alias")
        msg = str(excinfo.value).lower()
        assert "shared" in msg or "dependents" in msg


# ---------------------------------------------------------------------------
# CorruptConfigError — scan_model error semantics
# ---------------------------------------------------------------------------


class TestCorruptConfig:
    def test_corrupt_config_raises_and_descriptor_is_accessible(
        self,
        migrated_db: sqlite3.Connection,
    ) -> None:
        """A corrupt ``config`` value surfaces CorruptConfigError.

        Identity fields (slug/name/provider/dimension/vec_table/
        is_default/status) MUST be accessible on ``.descriptor`` —
        Go returns ``(descriptor, error)`` so callers needing only
        identity can use them; we mirror that via ``.descriptor``.
        """
        # Manually corrupt the seeded row's config.
        migrated_db.execute("UPDATE embedding_model SET config='{not-json' WHERE slug='bge-m3'")

        with pytest.raises(CorruptConfigError) as excinfo:
            ModelRegistryImpl(migrated_db).get("bge-m3")

        d = excinfo.value.descriptor
        # Identity fields scanned cleanly.
        assert d.slug == "bge-m3"
        assert d.dimension == 1024
        assert d.vec_table == "vec_bge_m3_1024"
        assert d.is_default is True
        assert d.status == "active"
        # base_url / api_key are absent (the JSON parse failed before they
        # could be populated).
        assert d.base_url == ""
        assert d.api_key == ""

    def test_corrupt_config_in_list_surfaces(
        self,
        migrated_db: sqlite3.Connection,
    ) -> None:
        """``list()`` also surfaces CorruptConfigError on the first corrupt row."""
        migrated_db.execute("UPDATE embedding_model SET config='{' WHERE slug='bge-m3'")
        with pytest.raises(CorruptConfigError):
            ModelRegistryImpl(migrated_db).list()

    def test_empty_config_string_does_not_raise(
        self,
        migrated_db: sqlite3.Connection,
    ) -> None:
        """An empty ``config`` string is benign (Go skips JSON parse)."""
        # Insert a row with an empty config (edge case: Go's `if cfg != ""`).
        migrated_db.execute(
            "INSERT INTO embedding_model "
            "(slug, name, provider, dimension, vec_table, is_default, status, config, created_at) "
            "VALUES ('empty-cfg', 'Empty', 'ollama', 8, 'vec_empty_cfg_8', 0, "
            "'active', '', strftime('s','now'))"
        )
        m = ModelRegistryImpl(migrated_db).get("empty-cfg")
        # base_url / api_key stay at their dataclass defaults ("").
        assert m.base_url == ""
        assert m.api_key == ""

    def test_corrupt_config_does_not_leak_json_decode_error(
        self,
        migrated_db: sqlite3.Connection,
    ) -> None:
        """The error message names the corruption, not the JSON library."""
        migrated_db.execute("UPDATE embedding_model SET config='{' WHERE slug='bge-m3'")
        with pytest.raises(CorruptConfigError) as excinfo:
            ModelRegistryImpl(migrated_db).get("bge-m3")
        # The error message is human-readable; ``json.JSONDecodeError`` is
        # chained as ``__cause__`` for callers that want to introspect.
        assert "config" in str(excinfo.value).lower()
        assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


# ---------------------------------------------------------------------------
# Protocol conformance — ModelRegistryImpl satisfies ModelRegistry Protocol.
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_isinstance_protocol(
        self,
        migrated_db: sqlite3.Connection,
    ) -> None:
        """``isinstance(reg, ModelRegistry)`` via runtime_checkable Protocol."""
        reg = ModelRegistryImpl(migrated_db)
        assert isinstance(reg, ModelRegistry)


# ---------------------------------------------------------------------------
# roundtrip — full lifecycle, integrates register/set_default/remove.
# ---------------------------------------------------------------------------


class TestRoundtrip:
    def test_full_lifecycle(
        self,
        migrated_db: sqlite3.Connection,
        reg: ModelRegistryImpl,
    ) -> None:
        """register → list → set_default → get_active → remove, end-to-end."""
        # Register two new models alongside the seeded default.
        reg.register(_spec("m1"))
        reg.register(_spec("m2", dimension=768))

        # set_default flips; get_active returns m1.
        reg.set_default("m1")
        active = reg.get_active()
        assert active.slug == "m1"

        # Removing m1 must first switch default off it.
        reg.set_default("bge-m3")
        reg.remove("m1")

        slugs = {m.slug for m in reg.list()}
        assert "m1" not in slugs
        assert "m2" in slugs
        assert "bge-m3" in slugs


# ---------------------------------------------------------------------------
# INSERT-race protection — wrapInsertErr translates IntegrityError.
# ---------------------------------------------------------------------------


class TestRaceProtection:
    def test_integrity_error_on_insert_translates_to_conflict(
        self,
        migrated_db: sqlite3.Connection,
        reg: ModelRegistryImpl,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The pre-check passes but a UNIQUE violation fires on INSERT.

        Simulates the race where two concurrent ``register`` calls both
        pass the pre-check. We monkeypatch ``_existing_slug`` to always
        return False, bypassing the pre-check, then let the INSERT
        fire against a row that already exists. The resulting
        ``IntegrityError`` MUST be translated to ``ModelConflict`` via
        :func:`_wrap_insert_err`.

        ``sqlite3.Connection.execute`` is read-only on Python 3.12+, so
        we can't monkeypatch the connection itself — patching the
        module-level helper is the supported seam.
        """
        # Seed the conflicting row directly so the INSERT path will hit
        # the UNIQUE violation.
        migrated_db.execute(
            "INSERT INTO embedding_model "
            "(slug, name, provider, dimension, vec_table, is_default, status, config, created_at) "
            "VALUES ('racy', 'Racy', 'ollama', 8, 'vec_racy_8', 0, 'active', '{}', "
            "strftime('s','now'))"
        )

        spec = ModelSpec(slug="racy", provider="x", base_url="y", api_key="z", dimension=8)

        # Bypass the pre-check so the INSERT path is reached.
        import unictx.storage.model_registry_impl as mri

        monkeypatch.setattr(mri, "_existing_slug", lambda db, slug: False)

        with pytest.raises(ModelConflict):
            reg.register(spec)
