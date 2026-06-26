"""Tests for `unictx.config` — Pydantic Config schema + YAML loader + XDG.

Mirrors Go's `internal/config/config_test.go` behavior coverage. Uses
`tmp_path` for filesystem isolation and `monkeypatch` for XDG env-var
manipulation so tests don't touch the caller's real `~/.config/unictx`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from unictx.config import (
    Config,
    EmbedderConfig,
    HttpPdfEngineConfig,
    PdfConfig,
    ShellPdfEngineConfig,
    UserConfig,
    load,
    xdg_config_home,
    xdg_data_home,
)

# ---------------------------------------------------------------------------
# load() — missing file
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_defaults(tmp_path: Path):
    """Non-existent path → Config() with all defaults applied."""
    cfg = load(tmp_path / "does-not-exist.yaml")

    assert cfg.user.id == "default"
    assert cfg.embedder.enabled is False
    assert cfg.embedder.provider == ""
    assert cfg.embedder.base_url == ""
    assert cfg.embedder.model == ""
    assert cfg.embedder.dimension == 0
    assert cfg.embedder.api_key == ""
    assert cfg.pdf.engine == ""
    # data_dir resolves via xdg_data_home() at call time — just check shape
    assert cfg.data_dir.name == "unictx"


def test_load_missing_file_path_matches_fresh_config(tmp_path: Path):
    """`load(missing)` should equal `Config()` field-for-field."""
    a = load(tmp_path / "nope.yaml")
    b = Config()
    assert a.model_dump() == b.model_dump()


# ---------------------------------------------------------------------------
# load() — XDG path resolution
# ---------------------------------------------------------------------------


def test_load_explicit_none_path_uses_xdg(tmp_path: Path, monkeypatch):
    """`load(None)` resolves via `$XDG_CONFIG_HOME/unictx/config.yaml`.

    Without a file there, defaults apply. We verify the resolution
    happened by writing a config at the resolved path and reading it
    back through `load(None)`.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg_dir = tmp_path / "unictx"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text("user:\n  id: alice\n", encoding="utf-8")

    cfg = load(None)
    assert cfg.user.id == "alice"


def test_load_explicit_none_path_missing_file_returns_defaults(tmp_path: Path, monkeypatch):
    """`load(None)` with no file at the resolved XDG path → defaults."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = load(None)
    assert cfg.user.id == "default"


# ---------------------------------------------------------------------------
# load() — valid YAML
# ---------------------------------------------------------------------------


def test_load_minimal_yaml(tmp_path: Path):
    """YAML with just `user.id` → that field set, everything else default."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("user:\n  id: alice\n", encoding="utf-8")

    cfg = load(cfg_path)

    assert cfg.user.id == "alice"
    # Other fields keep defaults
    assert cfg.embedder == EmbedderConfig()
    assert cfg.pdf == PdfConfig()


def test_load_full_yaml(tmp_path: Path):
    """YAML exercising every field across user/data_dir/embedder/pdf."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "user": {"id": "bob"},
                "data_dir": str(tmp_path / "data"),
                "embedder": {
                    "enabled": True,
                    "provider": "openai-compat",
                    "base_url": "https://api.example.com/v1",
                    "model": "text-embed-3-small",
                    "dimension": 1536,
                    "api_key": "sk-xxx",
                },
                "pdf": {
                    "engine": "http",
                    "engines": {
                        "shell": {"command": "pdftotext -layout - -", "timeout_seconds": 60},
                        "http": {
                            "url": "https://pdf.example.com/extract",
                            "timeout_seconds": 45,
                            "auth_token": "tok-xyz",
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load(cfg_path)

    assert cfg.user.id == "bob"
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.embedder.enabled is True
    assert cfg.embedder.provider == "openai-compat"
    assert cfg.embedder.base_url == "https://api.example.com/v1"
    assert cfg.embedder.model == "text-embed-3-small"
    assert cfg.embedder.dimension == 1536
    assert cfg.embedder.api_key == "sk-xxx"
    assert cfg.pdf.engine == "http"
    assert cfg.pdf.engines.shell.command == "pdftotext -layout - -"
    assert cfg.pdf.engines.shell.timeout_seconds == 60
    assert cfg.pdf.engines.http.url == "https://pdf.example.com/extract"
    assert cfg.pdf.engines.http.timeout_seconds == 45
    assert cfg.pdf.engines.http.auth_token == "tok-xyz"


def test_load_empty_yaml_returns_defaults(tmp_path: Path):
    """Empty YAML file (or `null`) → Config() with all defaults."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("", encoding="utf-8")

    cfg = load(cfg_path)
    assert cfg == Config()


def test_load_null_yaml_returns_defaults(tmp_path: Path):
    """YAML parsing `null` (`--- null`) → safe_load returns None → defaults."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("--- null\n", encoding="utf-8")

    cfg = load(cfg_path)
    assert cfg == Config()


# ---------------------------------------------------------------------------
# load() — validation failures
# ---------------------------------------------------------------------------


def test_load_invalid_yaml_raises_validation_error(tmp_path: Path):
    """Wrong-type field (`enabled: not-a-bool`) → ValidationError."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "embedder:\n  enabled: not-a-bool\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load(cfg_path)


def test_load_embedder_dimension_wrong_type_raises(tmp_path: Path):
    """`dimension: "lots"` → ValidationError."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        'embedder:\n  dimension: "lots"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load(cfg_path)


def test_load_data_dir_non_string_raises(tmp_path: Path):
    """`data_dir: 12345` (non-string scalar) → ValidationError.

    Pydantic v2's `Path` validator rejects non-string inputs outright
    (no silent int→str coercion). This is stricter than Go's yaml
    unmarshaler, which would have stringified — but Pydantic's behavior
    surfaces config typos at load time rather than producing a
    nonsensical path. Documented here so a future loosening is a
    conscious choice.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("data_dir: 12345\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load(cfg_path)


# ---------------------------------------------------------------------------
# XDG helpers
# ---------------------------------------------------------------------------


def test_xdg_data_home_env_var_honored(tmp_path: Path, monkeypatch):
    """`XDG_DATA_HOME` env var is honored by xdg_data_home()."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert xdg_data_home() == tmp_path


def test_xdg_config_home_env_var_honored(tmp_path: Path, monkeypatch):
    """`XDG_CONFIG_HOME` env var is honored by xdg_config_home()."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert xdg_config_home() == tmp_path


def test_xdg_data_home_falls_back_to_home(monkeypatch):
    """No XDG_DATA_HOME → ~/.local/share."""
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    expected = Path.home() / ".local" / "share"
    assert xdg_data_home() == expected


def test_xdg_config_home_falls_back_to_home(monkeypatch):
    """No XDG_CONFIG_HOME → ~/.config."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    expected = Path.home() / ".config"
    assert xdg_config_home() == expected


def test_config_data_dir_default_uses_xdg_data_home(monkeypatch, tmp_path: Path):
    """`Config()` without explicit data_dir composes `$XDG_DATA_HOME/unictx`."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config()
    assert cfg.data_dir == tmp_path / "unictx"


# ---------------------------------------------------------------------------
# EmbedderConfig — defaults gated on `enabled`
# ---------------------------------------------------------------------------


def test_embedder_defaults_applied_when_enabled(tmp_path: Path):
    """`embedder.enabled: true` (no other fields) → all defaults applied.

    Mirrors Go config.go:101-123.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("embedder:\n  enabled: true\n", encoding="utf-8")

    cfg = load(cfg_path)

    assert cfg.embedder.enabled is True
    assert cfg.embedder.provider == "ollama"
    assert cfg.embedder.base_url == "http://localhost:11434"
    assert cfg.embedder.model == "bge-m3"
    assert cfg.embedder.dimension == 1024


def test_embedder_defaults_NOT_applied_when_disabled(tmp_path: Path):
    """`embedder.enabled: false` → no defaults applied, fields stay empty."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "embedder:\n  enabled: false\n  model: ''\n",
        encoding="utf-8",
    )

    cfg = load(cfg_path)

    assert cfg.embedder.enabled is False
    assert cfg.embedder.provider == ""
    assert cfg.embedder.base_url == ""
    assert cfg.embedder.model == ""
    assert cfg.embedder.dimension == 0


def test_embedder_defaults_NOT_applied_when_field_absent():
    """EmbedderConfig() with no args → disabled + no defaults applied."""
    e = EmbedderConfig()
    assert e.enabled is False
    assert e.provider == ""
    assert e.model == ""
    assert e.dimension == 0


def test_embedder_user_values_NOT_overwritten_when_enabled(tmp_path: Path):
    """`embedder: {enabled: true, model: custom}` → user value preserved."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "embedder:\n  enabled: true\n  model: custom-model\n  provider: openai-compat\n",
        encoding="utf-8",
    )

    cfg = load(cfg_path)

    assert cfg.embedder.enabled is True
    assert cfg.embedder.provider == "openai-compat"
    # User-set provider triggers the openai-compat base_url default
    assert cfg.embedder.base_url == "http://localhost:1234/v1"
    # User-set model preserved
    assert cfg.embedder.model == "custom-model"
    # dimension still defaulted (user didn't set it)
    assert cfg.embedder.dimension == 1024


def test_embedder_user_dimension_NOT_overwritten_when_enabled(tmp_path: Path):
    """User-set dimension preserved even when enabled=true."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "embedder:\n  enabled: true\n  dimension: 768\n",
        encoding="utf-8",
    )

    cfg = load(cfg_path)
    assert cfg.embedder.dimension == 768


def test_embedder_defaults_applied_on_direct_construction():
    """Defaults apply on direct `EmbedderConfig(enabled=True)` too.

    The model_validator runs on every EmbedderConfig instantiation — not
    just when constructed via Config. This is the whole point of using a
    Pydantic validator over a Config-level post-load step.
    """
    e = EmbedderConfig(enabled=True)
    assert e.provider == "ollama"
    assert e.base_url == "http://localhost:11434"
    assert e.model == "bge-m3"
    assert e.dimension == 1024


def test_embedder_unknown_provider_leaves_base_url_empty():
    """Unknown provider string → base_url defaults to "" (per .get(..., ""))."""
    e = EmbedderConfig(enabled=True, provider="mystery-provider")
    assert e.base_url == ""


# ---------------------------------------------------------------------------
# PdfEnginesConfig — flat struct precise validation
# ---------------------------------------------------------------------------


def test_pdf_engines_default_values():
    """PdfConfig() defaults: engine="", shell/http sub-configs at defaults."""
    cfg = PdfConfig()
    assert cfg.engine == ""
    assert cfg.engines.shell == ShellPdfEngineConfig()
    assert cfg.engines.http == HttpPdfEngineConfig()


def test_pdf_engines_flat_struct_validates_precisely(tmp_path: Path):
    """`pdf.engines.shell.url` is rejected — ShellPdfEngineConfig has no `url`.

    This is the whole point of the flat struct: a heterogeneous
    `dict[str, A | B]` would silently accept `url` on a shell config by
    falling back to HttpPdfEngineConfig; the flat struct catches it.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "pdf": {
                    "engines": {
                        "shell": {
                            "url": "http://example.com/extract",
                            "command": "pdftotext - -",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load(cfg_path)


def test_pdf_engines_http_field_rejects_shell_only_key(tmp_path: Path):
    """`pdf.engines.http.command` is rejected — HttpPdfEngineConfig has no `command`."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "pdf": {
                    "engines": {
                        "http": {
                            "command": "pdftotext - -",
                            "url": "http://example.com",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load(cfg_path)


# ---------------------------------------------------------------------------
# UserConfig default
# ---------------------------------------------------------------------------


def test_user_config_default_id():
    """UserConfig() with no args → id='default' (Go: UserConfig{ID:"default"})."""
    u = UserConfig()
    assert u.id == "default"


def test_user_config_explicit_empty_string_falls_through(tmp_path: Path):
    """`user.id: ''` is NOT auto-defaulted to 'default'.

    Deviation from Go: Go's Load explicitly re-applies `id="default"`
    after parse when ID=="" (config.go:98-100). Pydantic's field default
    only applies when the field is *absent* — an explicit empty string
    passes through unchanged. This matches the brief's code (no
    validator), and the resulting config is trivially distinguishable
    from a default config via `cfg.user.id == ""`. CLI/UI consumers
    should treat empty user.id as "use a sensible default at use-site"
    rather than relying on this layer to coalesce.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("user:\n  id: ''\n", encoding="utf-8")

    cfg = load(cfg_path)
    # Documents ACTUAL behavior — if Go-parity is required, a follow-up
    # task would add a model_validator mirroring config.go:98-100.
    assert cfg.user.id == ""


# ---------------------------------------------------------------------------
# Strict mode (extra='forbid')
# ---------------------------------------------------------------------------


def test_config_rejects_unknown_top_level_field(tmp_path: Path):
    """Unknown top-level key (`typo: ...`) → ValidationError.

    All Config models forbid extra fields. This is the precise-validation
    contract that makes PdfEnginesConfig's flat struct meaningful and
    catches config typos at load time instead of silently dropping them.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "user:\n  id: alice\ntypo_field: surprise\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load(cfg_path)


def test_config_rejects_unknown_embedder_field(tmp_path: Path):
    """Unknown embedder field → ValidationError."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "embedder:\n  enabled: false\n  mistyped: x\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load(cfg_path)
