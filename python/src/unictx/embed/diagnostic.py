"""DiagnosticService — powers the ``doctor`` command.

Behavior-port of Go's ``internal/service/diagnostic.go``. Owns the
schema-version lookup (previously raw ``a.DB.QueryRow(...)`` in the
CLI) and the embedder health check (previously inline
``a.Embedder.Embed`` / ``.model()`` calls). Routing these through a
service means the inbound layer has no direct dependency on the
storage connection or port.Embedder — mirroring how ItemService owns
the get/list/delete path.

Plan §Python Conventions adaptations:
  - ctx dropped (Python is sync).
  - Go's ``PingEmbedder(ctx) (ModelInfo, bool, error)`` →
    ``ping_embedder() -> tuple[ModelInfo, bool]``; on failure raises
    rather than returning the error as a third value. Callers wrap
    in try/except for the "FAIL" branch.
  - Embedder is optional (None = Plan 1 / disabled).

A consumer-side ``SchemaMeta`` Protocol is defined locally — the
Phase 2 ``SchemaMetaImpl`` is the only known implementation, but
introducing the Protocol keeps the dependency structural (any class
with a ``version() -> str`` method satisfies it) and lets tests
inject stubs without subclassing the concrete impl.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from unictx.embed.embedder import Embedder, ModelInfo

__all__ = ["DiagnosticService", "SchemaMeta"]


@runtime_checkable
class SchemaMeta(Protocol):
    """Read-only accessor for ``schema_meta.schema_version``.

    Consumer-side Protocol — the concrete :class:`SchemaMetaImpl` is
    the production impl. Defined here (vs in storage/) because the
    consumer (DiagnosticService) owns what it needs; this matches the
    IngestService._Embedder pattern.
    """

    def version(self) -> str:
        """Return the migration version string.

        Raises SchemaMetaNotFound if the ``schema_version`` row is
        missing (uninitialized DB). Other failures propagate verbatim.
        """
        ...


class DiagnosticService:
    """Powers the ``doctor`` command.

    Construction is cheap (no I/O). ``embedder`` is optional — when
    None, ``ping_embedder`` reports disabled rather than attempting
    an embed call. This mirrors Plan 1 behavior: no embedder wired →
    ``doctor`` says "disabled" instead of "FAIL".
    """

    def __init__(
        self,
        schema: SchemaMeta,
        embedder: Embedder | None = None,
    ) -> None:
        self._schema = schema
        self._embedder = embedder

    def schema_version(self) -> str:
        """Return the migration version string.

        Errors propagate unwrapped so callers can distinguish a missing
        schema_meta table (uninitialized DB) from other failures.
        """
        return self._schema.version()

    def ping_embedder(self) -> tuple[ModelInfo, bool]:
        """Exercise the embedder with a one-token embed.

        Returns:
          - ``(zero, False)`` when no embedder is wired (Plan 1).
            Callers use ``enabled=False`` to print "disabled" rather
            than "FAIL".
          - ``(ModelInfo, True)`` when the embedder answered the ping.
            Callers print "<slug>, <dim>-dim".
          - On failure: raises the embedder's exception. ``enabled``
            stays True (caller catches the exception to print
            "FAIL (...)").

        ``model()`` is intentionally NOT called on failure — matches
        Go's behavior and avoids masking the embed error with a stale
        model label.
        """
        if self._embedder is None:
            return ModelInfo(), False
        # One-token embed to surface transient failures (Ollama down,
        # wrong base URL, auth reject) before they bite a real search.
        self._embedder.embed(["ping"])
        return self._embedder.model(), True
