"""Configuration discovery and path-resolution tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.config import discover_config, load_config, select_remote
from git_deploy.errors import ConfigurationError
from git_deploy.state import DeploymentStore


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


def test_named_remotes_apply_project_overrides_and_isolate_state(tmp_path: Path) -> None:
    """Resolve dev/prod roots and hooks without sharing deployment history."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        """
[remotes.dev]
protocol = "sftp"
host = "dev.example.invalid"

[remotes.prod]
protocol = "sftp"
host = "prod.example.invalid"

[projects.demo]
repository = "repository"
local_state_dir = ".state/demo"
post_commands = ["shared-command"]

[projects.demo.remotes.dev]
remote_root = "/srv/dev/demo"
post_commands = []
health_urls = ["https://dev.example.invalid/health"]

[projects.demo.remotes.prod]
remote_root = "/srv/prod/demo"
post_commands = ["restart-production"]
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)
    dev_name, dev_server, dev_projects = select_remote(config, "dev")
    prod_name, prod_server, prod_projects = select_remote(config, "prod")
    dev = dev_projects["demo"]
    prod = prod_projects["demo"]

    assert dev_name == "dev"
    assert prod_name == "prod"
    assert dev_server.values["host"] == "dev.example.invalid"
    assert prod_server.values["host"] == "prod.example.invalid"
    assert dev.remote_root == "/srv/dev/demo"
    assert prod.remote_root == "/srv/prod/demo"
    assert dev.post_commands == ()
    assert prod.post_commands == ("restart-production",)
    assert dev.health_urls == ("https://dev.example.invalid/health",)
    assert DeploymentStore(dev).root == tmp_path / ".state/demo/remotes/dev"
    assert DeploymentStore(prod).root == tmp_path / ".state/demo/remotes/prod"


def test_multiple_named_remotes_require_an_explicit_selection(tmp_path: Path) -> None:
    """Fail closed when omitting the remote could accidentally select production."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"

[remotes.prod]
protocol = "sftp"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    with pytest.raises(ConfigurationError, match="--remote is required"):
        select_remote(config, None)


def test_legacy_server_configuration_resolves_as_default_remote(tmp_path: Path) -> None:
    """Keep existing single-server configuration and state paths compatible."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)
    remote_name, _, projects = select_remote(config, None)
    project = projects["demo"]

    assert remote_name == "default"
    assert project.remote == "default"
    assert DeploymentStore(project).root == tmp_path / ".state/demo"
