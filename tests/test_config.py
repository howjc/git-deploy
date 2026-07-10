"""Configuration discovery and path-resolution tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.config import discover_config, load_config
from git_deploy.errors import ConfigurationError


def test_current_directory_deploy_toml_has_default_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use ``./deploy.toml`` before an environment-level fallback."""

    current = tmp_path / "current"
    fallback = tmp_path / "fallback.toml"
    current.mkdir()
    (current / "deploy.toml").write_text("[server]\n[projects.demo]\n", encoding="utf-8")
    fallback.write_text("[server]\n[projects.demo]\n", encoding="utf-8")
    monkeypatch.chdir(current)
    monkeypatch.setenv("GIT_DEPLOY_CONFIG", str(fallback))

    assert discover_config() == (current / "deploy.toml").resolve()


def test_load_config_resolves_project_paths_from_toml_directory(tmp_path: Path) -> None:
    """Resolve repository and local state paths relative to the TOML file."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        """
[server]
protocol = "sftp"

[projects.demo]
repository = "repository"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
include = ["src/**"]
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.projects["demo"].repository == repository.resolve()
    assert config.projects["demo"].local_state_dir == (tmp_path / ".state/demo").resolve()
    assert config.projects["demo"].include == ("src/**",)


def test_explicit_missing_config_is_rejected(tmp_path: Path) -> None:
    """Do not silently fall back when an explicit path is invalid."""

    with pytest.raises(ConfigurationError, match="does not exist"):
        discover_config(str(tmp_path / "missing.toml"))
