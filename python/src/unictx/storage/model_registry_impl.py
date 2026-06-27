"""SQLite-backed :class:`ModelRegistry` implementation.

Ports Go's ``archive/go/internal/adapter/sqlite/model_registry.go``.
This is the concrete storage-side impl of the
:class:`unictx.embed.model_registry.ModelRegistry` Protocol — it owns
the ``embedding_model`` table plus the per-slug ``vec_<slug>_<dim>``
virtual tables.

Method-by-method mapping to Go
==============================

* ``list`` — Go ``List``. ``ORDER BY created_at ASC, slug ASC``;
  returns ``[]`` (not None) if no rows.
* ``get_active`` — Go ``GetActive``. ``WHERE is_default=1 LIMIT 1``;
  raises :class:`ModelNotFound(slug="")` if no default.
* ``get`` — Go ``Get``. ``WHERE slug=?``; raises
  :class:`ModelNotFound(slug)`.
* ``register`` — Go ``Register``. Pre-check + INSERT + ``CREATE
  VIRTUAL TABLE`` in one transaction; raises :class:`ModelConflict`
  on duplicate (race-protected via :class:`sqlite3.IntegrityError`).
* ``update_config`` — Go ``UpdateConfig``. UPDATE; raises
  :class:`ModelNotFound` if no row affected.
* ``set_default`` — Go ``SetDefault``. Pre-check existence, then
  BEGIN / clear-old / set-new / COMMIT.
* ``remove`` — Go ``Remove``. Pre-check existence + is_default +
  shared-vec-table; then BEGIN / DROP / DELETE-status / DELETE-model /
  COMMIT.

Transaction handling
====================

The connection is in autocommit mode (``isolation_level=None`` set by
:func:`unictx.storage.db.open_db`). Multi-statement atomicity requires
explicit ``BEGIN``/``COMMIT`` — exactly the idiom Go uses via
``BeginTx``/``Commit``. Each multi-statement method
(register/set_default/remove) wraps itself in BEGIN/COMMIT with a
ROLLBACK on any exception.

scan_model + CorruptConfigError
===============================

Go's ``scanModel`` returns ``(ModelDescriptor, error)`` so callers that
only need identity (slug, dimension, vec_table) can keep the descriptor
even when the ``config`` JSON is unparseable. Python can't return
``(value, error)`` cleanly without losing the convenience of raising —
so the corrupt-config path **raises** :class:`CorruptConfigError` with
the partial descriptor attached as :attr:`descriptor`. This is the
documented Python-idiomatic equivalent: callers needing the partial
scan catch ``CorruptConfigError`` and read ``.descriptor``.

The error message is human-readable ("embedding_model.config corrupt:
<underlying>"); the underlying :class:`json.JSONDecodeError` is chained
via ``raise ... from err`` (preserving ``__cause__``) so callers that
want to introspect the parse failure can.

Deferral note
=============

The brief mentions ``reconcile_plan2c_sync``. **This is deferred** — it
is an app-layer orchestration function (Go: ``internal/app/app.go:275``)
that composes ``db + reg + cfg`` to self-heal Plan 2b alias rows on
startup. It does not belong in the storage layer. Deferred to Phase 7
(app wire-up).
"""

from __future__ import annotations

import json
import re
import sqlite3

from unictx.embed.errors import (
    CorruptConfigError,
    InvalidSlugError,
    ModelConflict,
    ModelNotFound,
)
from unictx.embed.model_registry import ModelDescriptor, ModelSpec
from unictx.errors import UnictxError

__all__ = ["ModelRegistryImpl"]


# Slug chars: ASCII letters, digits, dashes, underscores. Rejects
# shell-meta, semicolons, parens, quotes, whitespace — anything that
# could break out of an SQL identifier when interpolated into vec0 DDL
# via ``_vec_table_name``. Used by ``_validate_slug`` as a belt-and-braces
# guard against SQL injection (slug is user input via ``ModelSpec.slug``).
_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_slug(slug: str) -> None:
    """Reject slugs that aren't safe SQL identifiers after dash→underscore.

    Raises :class:`InvalidSlugError` (a :class:`ValueError` subclass) on
    a bad slug. Called at the top of :meth:`ModelRegistryImpl.register`
    and again inside :func:`_vec_table_name` as belt-and-braces — the
    latter protects any future caller that bypasses ``register``.
    """
    if not _SLUG_RE.match(slug):
        raise InvalidSlugError(slug)


# ---------------------------------------------------------------------------
# SQL constants — verbatim port of Go's model_registry.go.
# Column order MUST match the unpack in _scan_model below.
# ---------------------------------------------------------------------------

_SELECT_MODEL_COLS = "slug, name, provider, dimension, vec_table, is_default, status, config"

_LIST_SQL = f"SELECT {_SELECT_MODEL_COLS} FROM embedding_model ORDER BY created_at ASC, slug ASC"
_GET_ACTIVE_SQL = f"SELECT {_SELECT_MODEL_COLS} FROM embedding_model WHERE is_default = 1 LIMIT 1"
_GET_SQL = f"SELECT {_SELECT_MODEL_COLS} FROM embedding_model WHERE slug = ?"

_INSERT_SQL = """
INSERT INTO embedding_model
    (slug, name, provider, dimension, vec_table, is_default, status, config, created_at)
VALUES (?, ?, ?, ?, ?, 0, 'active', ?, strftime('%s','now'))
"""

_UPDATE_CONFIG_SQL = """
UPDATE embedding_model
SET provider = ?, config = ?
WHERE slug = ?
"""

_PRECHECK_SQL = "SELECT slug FROM embedding_model WHERE slug = ?"
_CLEAR_DEFAULTS_SQL = "UPDATE embedding_model SET is_default = 0 WHERE slug <> ?"
_SET_DEFAULT_SQL = "UPDATE embedding_model SET is_default = 1 WHERE slug = ?"
_LOAD_FOR_REMOVE_SQL = "SELECT is_default, vec_table FROM embedding_model WHERE slug = ?"
_COUNT_SHARED_VEC_SQL = "SELECT count(*) FROM embedding_model WHERE vec_table = ?"
_DROP_VEC_SQL_TMPL = "DROP TABLE IF EXISTS {table}"
_DELETE_STATUS_SQL = "DELETE FROM context_embedding WHERE model_slug = ?"
_DELETE_MODEL_SQL = "DELETE FROM embedding_model WHERE slug = ?"

# SQLite extended result code for UNIQUE constraint violations. Python's
# sqlite3 surfaces it as IntegrityError.sqlite_errorcode (3.11+).
# 2067 = SQLITE_CONSTRAINT_UNIQUE.
SQLITE_CONSTRAINT_UNIQUE = 2067


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vec_table_name(slug: str, dimension: int) -> str:
    """Derive the vec0 table name from slug + dimension.

    Mirrors Go's ``vecTableName`` verbatim: ``"vec_"`` + slug (with
    dashes replaced by underscores) + ``"_"`` + str(dimension). The
    result is a valid SQL identifier without quoting. Example:
    ``"text-embedding-3-large" @ 3072`` →
    ``"vec_text_embedding_3_large_3072"``.

    Validates *slug* via :func:`_validate_slug` as belt-and-braces: even
    though ``register`` already validates, any future caller that
    bypasses ``register`` (e.g. a heal path that rebuilds vec tables
    from a config dump) is still protected against SQL injection.
    """
    _validate_slug(slug)
    return f"vec_{slug.replace('-', '_')}_{dimension}"


def _config_json(base_url: str, api_key: str) -> str:
    """Encode ``{base_url, api_key}`` as compact JSON.

    Mirrors Go's ``json.Marshal(configJSON{...})``. Go's ``Marshal``
    emits no whitespace; we use compact separators to stay byte-identical
    (also matches :func:`unictx.storage.repo_impl._json_dumps`).
    """
    return json.dumps(
        {"base_url": base_url, "api_key": api_key},
        separators=(",", ":"),
    )


def _scan_model(row: tuple) -> ModelDescriptor:
    """Build a :class:`ModelDescriptor` from a raw row tuple.

    Mirrors Go's ``scanModel``. Column order matches :data:`_SELECT_MODEL_COLS`:

    0 slug, 1 name, 2 provider, 3 dimension, 4 vec_table,
    5 is_default (int), 6 status, 7 config (JSON text).

    On a corrupt ``config`` JSON value, **raises**
    :class:`CorruptConfigError` with the partial descriptor attached as
    :attr:`descriptor`. Identity fields (slug/name/provider/dimension/
    vec_table/is_default/status) are always populated on the partial;
    ``base_url``/``api_key`` stay at their dataclass defaults (``""``)
    when the parse fails. An empty ``config`` string is benign — Go's
    ``if cfg != ""`` guard, replicated here.
    """
    m = ModelDescriptor(
        slug=row[0],
        name=row[1],
        provider=row[2],
        dimension=row[3],
        vec_table=row[4],
        is_default=row[5] == 1,
        status=row[6],
    )
    cfg = row[7] or ""
    if cfg == "":
        # No config (or NULL) — leave base_url/api_key at the dataclass
        # defaults. Matches Go's ``if cfg != ""`` guard.
        return m
    try:
        parsed = json.loads(cfg)
    except json.JSONDecodeError as exc:
        # Go returns (descriptor, ErrCorruptConfig). Python equivalent:
        # raise CorruptConfigError with the partial descriptor attached
        # via .descriptor. Chained __cause__ preserves the JSON error.
        raise CorruptConfigError(
            f"embedding_model.config corrupt for slug {m.slug!r}: {exc.msg}",
            descriptor=m,
        ) from exc
    # configJSON in Go has exactly two keys; tolerant parse to allow
    # migration 0002's seed value (which adds an extra "model" key) to
    # scan without raising. Go silently ignores unknown keys; we do too.
    m.base_url = parsed.get("base_url", "")
    m.api_key = parsed.get("api_key", "")
    return m


def _existing_slug(db: sqlite3.Connection, slug: str) -> bool:
    """Return True if a row exists for *slug*. Mirrors Go's pre-check SELECT."""
    row = db.execute(_PRECHECK_SQL, (slug,)).fetchone()
    return row is not None


def _wrap_insert_err(err: sqlite3.Error, slug: str) -> Exception:
    """Translate a UNIQUE-violation IntegrityError to ModelConflict.

    Mirrors Go's ``wrapInsertErr``: detect
    ``sqlite3.ExtendedCode == sqlite3.ErrConstraintUnique`` and surface
    a domain conflict. Any other error is wrapped with context.
    """
    code = getattr(err, "sqlite_errorcode", None)
    if code == SQLITE_CONSTRAINT_UNIQUE:
        return ModelConflict(slug)
    # Defensive fallback: also match on the message string in case the
    # sqlite_errorcode attribute isn't populated on some Python builds.
    msg = str(err).lower()
    if "unique constraint failed" in msg and "slug" in msg:
        return ModelConflict(slug)
    return RuntimeError(f"insert model {slug!r}: {err}")


# ---------------------------------------------------------------------------
# ModelRegistryImpl
# ---------------------------------------------------------------------------


class ModelRegistryImpl:
    """SQLite-backed :class:`ModelRegistry`.

    Constructed with a :mod:`sqlite3` connection (typically produced by
    :func:`unictx.storage.db.open_db` and migrated via
    :func:`unictx.storage.migrations_runner.migrate`). Shares the
    connection with the rest of the storage layer.

    The connection is in autocommit mode (``isolation_level=None`` set
    by :func:`open_db`); multi-statement methods
    (:meth:`register`, :meth:`set_default`, :meth:`remove`) issue
    explicit ``BEGIN``/``COMMIT`` to keep their writes atomic.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    # ---- reads ----------------------------------------------------------

    def list(self) -> list[ModelDescriptor]:
        """All registered models, ordered by ``created_at ASC, slug ASC``.

        Empty list (not None) if no rows. Raises
        :class:`CorruptConfigError` on the first row whose ``config``
        JSON is unparseable (matches Go's ``scanModel`` propagation).
        """
        rows = self._db.execute(_LIST_SQL).fetchall()
        return [_scan_model(r) for r in rows]

    def get_active(self) -> ModelDescriptor:
        """The row with ``is_default=1``.

        Raises :class:`ModelNotFound(slug="")` if no row has the
        default flag set (mirrors Go's "no default model" error — the
        caller asked for "the active model" and there isn't one).
        """
        row = self._db.execute(_GET_ACTIVE_SQL).fetchone()
        if row is None:
            raise ModelNotFound("")
        return _scan_model(row)

    def get(self, slug: str) -> ModelDescriptor:
        """The row for *slug*. Raises :class:`ModelNotFound(slug)` if absent."""
        row = self._db.execute(_GET_SQL, (slug,)).fetchone()
        if row is None:
            raise ModelNotFound(slug)
        return _scan_model(row)

    # ---- writes ---------------------------------------------------------

    def register(self, spec: ModelSpec) -> None:
        """Insert a new model row + create its vec0 virtual table.

        Strict INSERT: raises :class:`ModelConflict(slug)` if *slug*
        already exists. A pre-check returns a clear error for the common
        case; the INSERT path also catches UNIQUE violations to handle
        the race between two concurrent ``register`` calls (mirrors
        Go's ``wrapInsertErr`` via :func:`_wrap_insert_err`).

        The INSERT and ``CREATE VIRTUAL TABLE`` are wrapped in a single
        ``BEGIN``/``COMMIT`` transaction — a partial failure (e.g. vec0
        extension unavailable) leaves the embedding_model row absent
        too, so a retry doesn't have to clean up a half-registered row.

        Raises :class:`InvalidSlugError` (a :class:`ValueError`) if the
        slug contains characters unsafe for SQL identifier use — the
        validation happens before the pre-check, before any SQL is
        issued, so a bad slug never reaches the DB.
        """
        # Validate slug FIRST: it flows into raw SQL via _vec_table_name
        # (CREATE VIRTUAL TABLE / DROP TABLE / vec0 DML). Defense in
        # depth against SQL injection from user-supplied ModelSpec.slug.
        _validate_slug(spec.slug)

        # Pre-check so we can return a clean ModelConflict instead of
        # relying on driver-specific constraint text (which differs
        # across sqlite versions). Mirrors Go's pre-check.
        if _existing_slug(self._db, spec.slug):
            raise ModelConflict(spec.slug)

        vec_table = _vec_table_name(spec.slug, spec.dimension)
        cfg = _config_json(spec.base_url, spec.api_key)

        create_sql = (
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {vec_table} USING vec0("
            f"item_id TEXT PRIMARY KEY, "
            f"embedding FLOAT[{spec.dimension}] distance_metric=cosine"
            f")"
        )

        self._db.execute("BEGIN")
        try:
            self._db.execute(
                _INSERT_SQL,
                (spec.slug, spec.slug, spec.provider, spec.dimension, vec_table, cfg),
            )
            self._db.execute(create_sql)
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            self._db.execute("ROLLBACK")
            raise _wrap_insert_err(exc, spec.slug) from exc
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def update_config(
        self,
        slug: str,
        base_url: str,
        api_key: str,
        provider: str,
    ) -> None:
        """Overwrite ``provider`` + ``config`` JSON for *slug*.

        Used to heal Plan 2b alias rows whose ``config`` was ``"{}"``.
        Raises :class:`ModelNotFound(slug)` if no row matches (the
        UPDATE affected 0 rows).
        """
        cfg = _config_json(base_url, api_key)
        cur = self._db.execute(_UPDATE_CONFIG_SQL, (provider, cfg, slug))
        if cur.rowcount == 0:
            raise ModelNotFound(slug)

    def set_default(self, slug: str) -> None:
        """Flip ``is_default`` atomically: *slug* → 1, all others → 0.

        Idempotent if *slug* is already the default. Raises
        :class:`ModelNotFound(slug)` if *slug* doesn't exist (the
        pre-check returns ErrNotFound rather than letting the UPDATE
        silently no-op).
        """
        if not _existing_slug(self._db, slug):
            raise ModelNotFound(slug)

        self._db.execute("BEGIN")
        try:
            self._db.execute(_CLEAR_DEFAULTS_SQL, (slug,))
            self._db.execute(_SET_DEFAULT_SQL, (slug,))
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def remove(self, slug: str) -> None:
        """Drop *slug*'s vec table + delete the model row in one transaction.

        Raises:
            ModelNotFound: *slug* is not registered.
            UnictxError (domain): *slug* is the current default
                (caller must :meth:`set_default` to a different model
                first). The message includes the word "default" or
                "switch" so callers can introspect.
            UnictxError (domain): *slug*'s ``vec_table`` is shared with
                another model (Plan 2b alias protection — dropping
                would corrupt the other model's vectors). The message
                includes "shared" or "dependents".

        The explicit ``DELETE FROM context_embedding`` is defense in
        depth — after migration 0004 the ``model_slug`` FK is
        ``ON DELETE CASCADE``, but this DELETE ensures correctness on
        DBs that pre-date 0004 or have FK enforcement off.
        """
        row = self._db.execute(_LOAD_FOR_REMOVE_SQL, (slug,)).fetchone()
        if row is None:
            raise ModelNotFound(slug)
        is_default, vec_table = row[0], row[1]
        if is_default == 1:
            raise _DefaultModelRemovalError(slug)

        (shared,) = self._db.execute(_COUNT_SHARED_VEC_SQL, (vec_table,)).fetchone()
        if shared > 1:
            raise _SharedVecTableError(vec_table, int(shared))

        drop_sql = _DROP_VEC_SQL_TMPL.format(table=vec_table)
        self._db.execute("BEGIN")
        try:
            self._db.execute(drop_sql)
            self._db.execute(_DELETE_STATUS_SQL, (slug,))
            self._db.execute(_DELETE_MODEL_SQL, (slug,))
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------------------
# Domain errors for the remove() guards.
#
# These are not raised by callers outside this module — they're internal
# to remove()'s refusal paths. Defined as named subclasses of UnictxError
# so the test suite can introspect the message AND catch UnictxError
# broadly. Mirrors Go's two fmt.Errorf calls in Remove; we promote them
# to named classes for cleaner Python idioms.
# ---------------------------------------------------------------------------


class _DefaultModelRemovalError(UnictxError):
    """``remove`` was called on a row with ``is_default=1``."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"cannot remove default model {slug!r}; switch default first")
        self.slug = slug


class _SharedVecTableError(UnictxError):
    """``remove`` was called on a row whose ``vec_table`` is shared.

    Plan 2b alias protection: dropping the table would corrupt the
    other model's vectors.
    """

    def __init__(self, vec_table: str, shared: int) -> None:
        super().__init__(
            f"vec table {vec_table!r} shared by {shared} models; remove dependents first"
        )
        self.vec_table = vec_table
        self.shared = shared
