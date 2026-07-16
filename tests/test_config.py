"""Configuration validation and mandatory protection tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.config import is_source_managed, load_config, path_matches
from git_deploy.errors import ConfigError
from tests.conftest import write_config


def test_loads_minimal_config_with_safe_defaults(git_project: Path) -> None:
    """A minimal target resolves while mandatory sensitive paths stay excluded."""

    config = load_config(write_config(git_project))

    assert config.default_target == "dev"
    assert config.target(None).fingerprint == "sftp:deploy@example.invalid:22:/srv/app"
    assert is_source_managed("app.py", config.source)
    assert not is_source_managed(".env.production", config.source)
    assert not is_source_managed("uploads/avatar.png", config.source)
    assert not is_source_managed("storage/cert/server.pem", config.source)
    assert not is_source_managed("private.key", config.source)


def test_rejects_legacy_configuration(git_project: Path) -> None:
    """v0.x project tables fail explicitly instead of entering compatibility mode."""

    path = git_project / "deploy.toml"
    path.write_text("[projects.app]\nrepository = '.'\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="not compatible"):
        load_config(path)


def test_ftp_requires_password_environment_name(git_project: Path) -> None:
    """FTP cannot silently fall back to a plaintext or anonymous password."""

    path = write_config(
        git_project,
        """
[targets.prod]
protocol = "ftp"
host = "ftp.example.invalid"
username = "deploy"
remote_root = "/public_html"
""",
    )

    with pytest.raises(ConfigError, match="password_env"):
        load_config(path)


def test_rejects_output_path_escape(git_project: Path) -> None:
    """Output scanning cannot be configured outside the project root."""

    path = write_config(
        git_project,
        """
[[outputs]]
local = "../secret"
remote = "public"

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
    )

    with pytest.raises(ConfigError, match="inside the project"):
        load_config(path)


def test_rejects_unknown_or_plaintext_password_fields(git_project: Path) -> None:
    """Typos and plaintext password keys fail instead of being silently ignored."""

    path = write_config(
        git_project,
        """
[targets.prod]
protocol = "ftp"
host = "ftp.example.invalid"
username = "deploy"
password_env = "FTP_PASSWORD"
password = "must-not-be-accepted"
remote_root = "/public_html"
""",
    )

    with pytest.raises(ConfigError, match="unknown targets.prod field"):
        load_config(path)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("public/assets", "public/assets"),
        ("public/assets", "public/assets/js"),
        (".", "vendor"),
    ],
)
def test_rejects_equal_or_nested_output_remote_roots(
    git_project: Path,
    left: str,
    right: str,
) -> None:
    """Output ownership is unambiguous even before either directory has files."""

    path = write_config(
        git_project,
        f"""
[[outputs]]
local = "dist"
remote = "{left}"

[[outputs]]
local = "vendor"
remote = "{right}"

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
    )

    with pytest.raises(ConfigError, match="equal or nested"):
        load_config(path)


@pytest.mark.parametrize("field", ["host", "username", "port", "password_env", "key_file"])
def test_native_alias_rejects_paramiko_connection_fields(
    git_project: Path,
    field: str,
) -> None:
    """Native alias targets cannot mix a second, conflicting connection model."""

    values = {
        "host": '"host"',
        "username": '"deploy"',
        "port": "2222",
        "password_env": '"PASSWORD"',
        "key_file": '"~/.ssh/id_ed25519"',
    }
    path = write_config(
        git_project,
        f"""
[targets.dev]
protocol = "sftp"
ssh_host_alias = "project-dev"
{field} = {values[field]}
remote_root = "/srv/app"
""",
    )

    with pytest.raises(ConfigError, match="Native OpenSSH.*conflicts"):
        load_config(path)


@pytest.mark.parametrize("name", ["build", "doctor", "init"])
def test_rejects_flat_cli_reserved_target_names(git_project: Path, name: str) -> None:
    """Target names cannot be shadowed by the CLI's action words."""

    path = write_config(
        git_project,
        f"""
[targets.{name}]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
    )

    with pytest.raises(ConfigError, match="reserved by the flat CLI"):
        load_config(path)


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("app.py", "**", True),
        ("runtime", "runtime/**", True),
        ("runtime/cache/a", "runtime/**", True),
        ("public/app.js", "*.js", True),
        ("public/app.css", "*.js", False),
    ],
)
def test_path_matching(path: str, pattern: str, expected: bool) -> None:
    """Project globs consistently handle recursive and basename patterns."""

    assert path_matches(path, (pattern,)) is expected
