"""Pydantic Config schema + YAML loader + XDG path helpers.

Ports Go's `archive/go/internal/config/config.go`. The Go struct field
layout is authoritative — every Go field has a Python counterpart. YAML
keys match Go's `yaml:` tags byte-for-byte so existing config files
round-trip between the Go and Python builds.

XDG precedence (mirrors Go's `defaultDataDir` / `DefaultConfigDir`):
  - `XDG_DATA_HOME`  → `$XDG_DATA_HOME/unictx`  (else `~/.local/share/unictx`)
  - `XDG_CONFIG_HOME` → `$XDG_CONFIG_HOME/unictx` (else `~/.config/unictx`)
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# All Config models forbid extra fields. This is the precise-validation
# contract the plan spec requires: an `http.url` typo under
# `engines.shell` is caught instead of silently stored, and a
# heterogeneous-dict-style silent fallback between engine sub-configs
# cannot happen. See plan §Task 1.4 PdfEnginesConfig rationale.
_STRICT = ConfigDict(extra="forbid")


def xdg_data_home() -> Path:
    """XDG_DATA_HOME → ~/.local/share.

    Mirrors Go's `defaultDataDir` (config.go:146-155) minus the trailing
    `/unictx` — callers compose that themselves so the helper stays
    orthogonal to the Go function shape.
    """
    if env := os.environ.get("XDG_DATA_HOME"):
        return Path(env).expanduser()
    return Path.home() / ".local" / "share"


def xdg_config_home() -> Path:
    """XDG_CONFIG_HOME → ~/.config.

    Mirrors Go's `DefaultConfigDir` (config.go:135-144) minus the trailing
    `/unictx`.
    """
    if env := os.environ.get("XDG_CONFIG_HOME"):
        return Path(env).expanduser()
    return Path.home() / ".config"


class UserConfig(BaseModel):
    """Owner identity for new items. Default 'default'.

    Mirrors Go's `UserConfig` struct. Go's Load applies `id="default"`
    when the field is empty after parsing (config.go:98-100); Pydantic's
    field default reproduces that without a post-load step.
    """

    model_config = _STRICT
    id: str = "default"


class EmbedderConfig(BaseModel):
    """Controls optional embedding pipeline.

    When enabled=False (the default), the app behaves as Plan 1: no
    vector indexing, search defaults to fts-only. When enabled=True,
    `apply_defaults` fills provider/base_url/model/dimension if empty
    (mirrors Go config.go:101-123).

    Binding-parity note: Go switches `base_url` on `provider in
    {ollama, openai}`; the plan spec uses `"openai-compat"` as the
    second provider key (see plan §Binding Decisions). We follow the
    plan spec verbatim.
    """

    model_config = _STRICT
    enabled: bool = False
    provider: str = ""
    base_url: str = ""
    model: str = ""
    dimension: int = 0
    api_key: str = ""

    @model_validator(mode="after")
    def apply_defaults(self) -> EmbedderConfig:
        if not self.enabled:
            return self
        if self.provider == "":
            self.provider = "ollama"
        if self.base_url == "":
            self.base_url = {
                "ollama": "http://localhost:11434",
                "openai-compat": "http://localhost:1234/v1",
            }.get(self.provider, "")
        if self.model == "":
            self.model = "bge-m3"
        if self.dimension == 0:
            self.dimension = 1024
        return self


class ShellPdfEngineConfig(BaseModel):
    """Shell-PDF engine config. Mirrors Go's EngineConfig when engine=shell.

    Go collapses shell + http into one struct with disjoint fields; the
    plan spec splits them so Pydantic validates precisely (an `http.url`
    typo under `engines.shell` is caught instead of silently stored).
    """

    model_config = _STRICT
    command: str = "pdftotext - -"
    timeout_seconds: int = 30


class HttpPdfEngineConfig(BaseModel):
    """HTTP PDF engine config. Mirrors Go's EngineConfig when engine=http."""

    model_config = _STRICT
    url: str = "http://localhost:8000/extract"
    timeout_seconds: int = 30
    auth_token: str = ""


class PdfEnginesConfig(BaseModel):
    """Mirrors Go's nested engines map — one field per engine.

    Go uses `map[string]EngineConfig`; the plan spec uses a flat struct
    so Pydantic validates each sub-config precisely. A heterogeneous
    `dict[str, A | B]` would let Pydantic silently fall back between
    types on mismatch, hiding config errors (e.g. a `url` typo under
    `shell` would be accepted as a shell-engine config).
    """

    model_config = _STRICT
    shell: ShellPdfEngineConfig = Field(default_factory=ShellPdfEngineConfig)
    http: HttpPdfEngineConfig = Field(default_factory=HttpPdfEngineConfig)


class PdfConfig(BaseModel):
    """PDF extraction config. Mirrors Go's `PDFConfig` struct."""

    model_config = _STRICT
    engine: str = ""
    engines: PdfEnginesConfig = Field(default_factory=PdfEnginesConfig)


class Config(BaseModel):
    """Root config. Mirrors Go's `Config` struct.

    `data_dir` uses a `default_factory` so `Config.model_validate({})`
    resolves correctly even when YAML omits the field. Go applies
    `defaultDataDir()` post-load (config.go:95-97) when the YAML leaves
    it empty; Pydantic's field default reproduces that for the empty
    case without a post-load step.
    """

    model_config = _STRICT
    user: UserConfig = Field(default_factory=UserConfig)
    data_dir: Path = Field(default_factory=lambda: xdg_data_home() / "unictx")
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    pdf: PdfConfig = Field(default_factory=PdfConfig)


def load(path: Path | None = None) -> Config:
    """Load config from YAML, falling back to defaults.

    Mirrors Go's `Load` (config.go:82-124): missing file → defaults;
    present file → `yaml.safe_load` + Pydantic validation. Pydantic's
    `ValidationError` propagates to the caller — Go surfaces a wrapped
    error, but callers in Python catch the rich `ValidationError` for
    structured reporting.

    Args:
        path: Explicit config file path, or None to resolve via XDG
            (`$XDG_CONFIG_HOME/unictx/config.yaml`).

    Returns:
        Validated `Config`. Defaults apply via field factories when the
        file is missing or fields are absent.
    """
    if path is None:
        path = xdg_config_home() / "unictx" / "config.yaml"
    if not path.exists():
        return Config()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Config.model_validate(data)
