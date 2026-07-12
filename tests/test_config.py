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


def test_target_id_remote_identity_shared_and_isolated(tmp_path: Path) -> None:
    """Named remotes map to physical target_id; username/alias excluded from payload."""

    from git_deploy.config import resolve_project_target
    from git_deploy.target_identity import build_physical_payload, resolve_target_identity

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "App.Example.COM."
username = "dev-user"
port = 22

[remotes.prod]
protocol = "sftp"
host = "app.example.com"
username = "prod-user"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo//"
target_id = "shared-demo"
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    _, dev_server, dev_projects = select_remote(config, "dev")
    _, prod_server, prod_projects = select_remote(config, "prod")

    dev_id = resolve_project_target(dev_server, dev_projects["demo"])
    prod_id = resolve_project_target(prod_server, prod_projects["demo"])
    # Same canonical payload + explicit id → shared physical target.
    assert dev_id.target_id == "shared-demo"
    assert prod_id.target_id == "shared-demo"
    assert dev_id.physical_fingerprint == prod_id.physical_fingerprint

    # Different protocol with the same explicit id is rejected at load time.
    other_payload = build_physical_payload(
        protocol="ftp",
        host="app.example.com",
        project="demo",
        remote_root="/srv/demo",
    )
    with pytest.raises(ConfigurationError, match="cannot merge distinct physical"):
        resolve_target_identity(
            {"protocol": "ftp", "host": "app.example.com"},
            "demo",
            remote_root="/srv/demo",
            explicit_target_id="shared-demo",
            bound_payload=dev_id.payload,
        )
    assert other_payload.fingerprint() != dev_id.physical_fingerprint


def test_remote_identity_without_explicit_id_derives_from_payload(tmp_path: Path) -> None:
    """Without target_id, different roots yield different derived ids."""

    from git_deploy.config import resolve_project_target

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "h.example"

[remotes.prod]
protocol = "sftp"
host = "h.example"

[projects.demo]
repository = "{repository}"
local_state_dir = ".state/demo"

[projects.demo.remotes.dev]
remote_root = "/srv/dev"

[projects.demo.remotes.prod]
remote_root = "/srv/prod"
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    _, dev_server, dev_projects = select_remote(config, "dev")
    _, prod_server, prod_projects = select_remote(config, "prod")
    dev_id = resolve_project_target(dev_server, dev_projects["demo"])
    prod_id = resolve_project_target(prod_server, prod_projects["demo"])
    assert dev_id.target_id != prod_id.target_id
    assert dev_id.state_root(tmp_path / ".state/demo").name == dev_id.target_id


def test_explicit_target_id_collision_rejected_on_load(tmp_path: Path) -> None:
    """Real deploy.toml load rejects distinct physical payloads sharing an explicit id.

    Must fail before any remote connect or state-dir access.
    """

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "dev.example"

[remotes.prod]
protocol = "sftp"
host = "prod.example"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
target_id = "forced-shared"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="cannot merge distinct physical"):
        load_config(config_path)
    # No state directory access should have been required.
    assert not (tmp_path / ".state").exists()


def test_explicit_target_id_collision_different_roots_on_load(tmp_path: Path) -> None:
    """Same host but different remote_root with one explicit id fails at load."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "app.example"

[remotes.prod]
protocol = "sftp"
host = "app.example"

[projects.demo]
repository = "{repository}"
target_id = "forced-shared"

[projects.demo.remotes.dev]
remote_root = "/srv/dev"

[projects.demo.remotes.prod]
remote_root = "/srv/prod"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="cannot merge distinct physical"):
        load_config(config_path)


def test_ftps_effective_port_matches_transport_default() -> None:
    """FTPS identity default effective port equals transport connect default (21)."""

    from git_deploy.target_identity import (
        build_physical_payload,
        default_port_for_protocol,
        effective_port,
    )

    assert default_port_for_protocol("ftps") == 21
    assert effective_port("ftps", None) == 21
    payload = build_physical_payload(
        protocol="ftps",
        host="ftp.example",
        project="demo",
        remote_root="/srv",
    )
    assert payload.port == 21
    # Explicit override still works.
    assert effective_port("ftps", 990) == 990
