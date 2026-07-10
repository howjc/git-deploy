"""Configuration discovery and TOML parsing."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ConfigurationError
from .models import AppConfig, ProjectConfig, ServerConfig


def discover_config(explicit: str | None = None) -> Path:
    """Resolve the deployment configuration without searching parent folders.

    Args:
        explicit: Optional path supplied by ``--config``.

    Returns:
        Absolute path to an existing TOML configuration file.

    Raises:
        ConfigurationError: When no candidate exists.
    """

    candidates: list[tuple[str, Path]] = []
    if explicit:
        candidates.append(("--config", Path(explicit).expanduser()))
    else:
        candidates.append(("current directory", Path.cwd() / "deploy.toml"))
        env_path = os.environ.get("GIT_DEPLOY_CONFIG")
        if env_path:
            candidates.append(("GIT_DEPLOY_CONFIG", Path(env_path).expanduser()))
        candidates.append(("user config", Path("~/.config/git-deploy/deploy.toml").expanduser()))

    for source, candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
        if explicit:
            raise ConfigurationError(f"configuration from {source} does not exist: {resolved}")

    raise ConfigurationError(
        "deploy.toml not found in the current directory; use --config or GIT_DEPLOY_CONFIG"
    )


def load_config(path: Path) -> AppConfig:
    """Parse and resolve one deployment TOML file.

    Args:
        path: Existing TOML file selected by configuration discovery.

    Returns:
        Validated application configuration.
    """

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc

    server = raw.get("server")
    if not isinstance(server, dict):
        raise ConfigurationError("configuration requires a [server] table")

    raw_projects = raw.get("projects")
    if not isinstance(raw_projects, dict) or not raw_projects:
        raise ConfigurationError("configuration requires at least one [projects.NAME] table")

    projects: dict[str, ProjectConfig] = {}
    for name, values in raw_projects.items():
        if not isinstance(values, dict):
            raise ConfigurationError(f"projects.{name} must be a table")
        projects[name] = _load_project(name, values, path.parent)

    return AppConfig(path=path, server=ServerConfig(dict(server)), projects=projects)


def _load_project(name: str, values: dict[str, Any], base: Path) -> ProjectConfig:
    """Resolve one project table relative to the configuration directory.

    Args:
        name: Project key used by the CLI.
        values: Raw TOML project mapping.
        base: Directory containing the TOML file.

    Returns:
        Validated project configuration.
    """

    raw_repository = values.get("repository", values.get("source"))
    remote_root = str(values.get("remote_root", "")).strip()
    if not raw_repository:
        raise ConfigurationError(f"projects.{name}.repository is required")
    if not remote_root.startswith("/"):
        raise ConfigurationError(f"projects.{name}.remote_root must be an absolute POSIX path")
    remote_parts = PurePosixPath(remote_root).parts
    if ".." in remote_parts or "." in remote_parts:
        raise ConfigurationError(f"projects.{name}.remote_root cannot contain traversal segments")

    repository = Path(str(raw_repository)).expanduser()
    if not repository.is_absolute():
        repository = base / repository
    repository = repository.resolve()
    if not repository.is_dir():
        raise ConfigurationError(f"projects.{name}.repository does not exist: {repository}")

    state_value = values.get("local_state_dir")
    state_dir = _resolve_optional_path(state_value, base)

    return ProjectConfig(
        name=name,
        repository=repository,
        remote_root=remote_root.rstrip("/"),
        include=_string_tuple(values.get("include"), ("**",)),
        exclude=_string_tuple(values.get("exclude"), ()),
        protected=_string_tuple(values.get("protected"), ()),
        post_commands=_string_tuple(values.get("post_commands"), ()),
        health_urls=_string_tuple(values.get("health_urls"), ()),
        local_state_dir=state_dir,
    )


def _resolve_optional_path(value: Any, base: Path) -> Path | None:
    """Resolve an optional path value relative to a configuration directory.

    Args:
        value: TOML value or ``None``.
        base: Directory containing the TOML file.

    Returns:
        Absolute path, or ``None`` when no value was configured.
    """

    if value is None:
        return None
    path = Path(str(value)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _string_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize a TOML string array.

    Args:
        value: Parsed TOML value.
        default: Value returned when the key is absent.

    Returns:
        Tuple of non-empty strings.
    """

    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError("expected an array of strings")
    return tuple(item for item in value if item)
