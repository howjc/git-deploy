"""Configuration discovery and TOML parsing."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ConfigurationError
from .models import AppConfig, ProjectConfig, ProjectRemoteConfig, ServerConfig


_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


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
    raw_remotes = raw.get("remotes")
    if server is not None and raw_remotes is not None:
        raise ConfigurationError("configure either [server] or [remotes.NAME], not both")
    if server is not None:
        if not isinstance(server, dict):
            raise ConfigurationError("server must be a table")
        remotes = {"default": ServerConfig(dict(server))}
        default_remote: str | None = "default"
    else:
        if not isinstance(raw_remotes, dict) or not raw_remotes:
            raise ConfigurationError("configuration requires [server] or at least one [remotes.NAME]")
        remotes = {}
        for name, values in raw_remotes.items():
            _validate_remote_name(name)
            if not isinstance(values, dict):
                raise ConfigurationError(f"remotes.{name} must be a table")
            remotes[name] = ServerConfig(dict(values))
        configured_default = raw.get("default_remote")
        default_remote = str(configured_default).strip() if configured_default is not None else None
        if default_remote is not None and default_remote not in remotes:
            raise ConfigurationError(f"unknown default_remote {default_remote!r}")

    raw_projects = raw.get("projects")
    if not isinstance(raw_projects, dict) or not raw_projects:
        raise ConfigurationError("configuration requires at least one [projects.NAME] table")

    projects: dict[str, ProjectConfig] = {}
    for name, values in raw_projects.items():
        if not isinstance(values, dict):
            raise ConfigurationError(f"projects.{name} must be a table")
        projects[name] = _load_project(name, values, path.parent, set(remotes))

    return AppConfig(
        path=path,
        remotes=remotes,
        projects=projects,
        default_remote=default_remote,
    )


def _load_project(
    name: str,
    values: dict[str, Any],
    base: Path,
    remote_names: set[str],
) -> ProjectConfig:
    """Resolve one project table relative to the configuration directory.

    Args:
        name: Project key used by the CLI.
        values: Raw TOML project mapping.
        base: Directory containing the TOML file.
        remote_names: Configured remote names available to the project.

    Returns:
        Validated project configuration.
    """

    raw_repository = values.get("repository", values.get("source"))
    remote_root = str(values.get("remote_root", "")).strip()
    if not raw_repository:
        raise ConfigurationError(f"projects.{name}.repository is required")
    if remote_root:
        remote_root = _validate_remote_root(remote_root, f"projects.{name}.remote_root")

    raw_project_remotes = values.get("remotes", {})
    if not isinstance(raw_project_remotes, dict):
        raise ConfigurationError(f"projects.{name}.remotes must be a table")
    project_remotes: dict[str, ProjectRemoteConfig] = {}
    for remote_name, overrides in raw_project_remotes.items():
        _validate_remote_name(remote_name)
        if remote_name not in remote_names:
            raise ConfigurationError(
                f"projects.{name}.remotes.{remote_name} has no matching [remotes.{remote_name}]"
            )
        if not isinstance(overrides, dict):
            raise ConfigurationError(f"projects.{name}.remotes.{remote_name} must be a table")
        override_root = str(overrides.get("remote_root", "")).strip() or None
        if override_root is not None:
            override_root = _validate_remote_root(
                override_root,
                f"projects.{name}.remotes.{remote_name}.remote_root",
            )
        project_remotes[remote_name] = ProjectRemoteConfig(
            remote_root=override_root,
            post_commands=(
                _string_tuple(overrides.get("post_commands"), ())
                if "post_commands" in overrides
                else None
            ),
            health_urls=(
                _string_tuple(overrides.get("health_urls"), ())
                if "health_urls" in overrides
                else None
            ),
        )

    missing_roots = sorted(
        remote_name
        for remote_name in remote_names
        if not remote_root
        and (
            remote_name not in project_remotes
            or project_remotes[remote_name].remote_root is None
        )
    )
    if missing_roots:
        raise ConfigurationError(
            f"projects.{name} requires remote_root for remote(s): {', '.join(missing_roots)}"
        )

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
        remote_root=remote_root,
        include=_string_tuple(values.get("include"), ("**",)),
        exclude=_string_tuple(values.get("exclude"), ()),
        protected=_string_tuple(values.get("protected"), ()),
        post_commands=_string_tuple(values.get("post_commands"), ()),
        health_urls=_string_tuple(values.get("health_urls"), ()),
        local_state_dir=state_dir,
        remotes=project_remotes,
    )


def select_remote(
    config: AppConfig,
    requested: str | None,
) -> tuple[str, ServerConfig, dict[str, ProjectConfig]]:
    """Resolve one remote and materialize its project-specific overrides.

    Args:
        config: Loaded application configuration.
        requested: CLI remote name, or ``None`` to use a safe configured default.

    Returns:
        Remote name, server settings, and resolved projects.
    """

    remote_name = requested or config.default_remote
    if remote_name is None:
        if len(config.remotes) == 1:
            remote_name = next(iter(config.remotes))
        else:
            available = ", ".join(config.remotes)
            raise ConfigurationError(
                f"--remote is required; available remotes: {available}"
            )
    try:
        server = config.remotes[remote_name]
    except KeyError as exc:
        available = ", ".join(config.remotes)
        raise ConfigurationError(
            f"unknown remote {remote_name!r}; available: {available}"
        ) from exc

    projects: dict[str, ProjectConfig] = {}
    for name, project in config.projects.items():
        override = project.remotes.get(remote_name)
        projects[name] = replace(
            project,
            remote_root=(override.remote_root if override and override.remote_root else project.remote_root),
            post_commands=(
                override.post_commands
                if override and override.post_commands is not None
                else project.post_commands
            ),
            health_urls=(
                override.health_urls
                if override and override.health_urls is not None
                else project.health_urls
            ),
            remote=remote_name,
            remotes={},
        )
    return remote_name, server, projects


def _validate_remote_name(name: str) -> None:
    """Reject remote names that are unsafe in CLI output or state paths.

    Args:
        name: Candidate remote identifier.

    Returns:
        None.
    """

    if not _REMOTE_NAME.fullmatch(name):
        raise ConfigurationError(f"invalid remote name: {name!r}")


def _validate_remote_root(value: str, field: str) -> str:
    """Validate and normalize one absolute POSIX deployment root.

    Args:
        value: Candidate remote directory.
        field: Configuration field name used in validation errors.

    Returns:
        Normalized absolute remote directory.
    """

    if not value.startswith("/"):
        raise ConfigurationError(f"{field} must be an absolute POSIX path")
    parts = PurePosixPath(value).parts
    if ".." in parts or "." in parts:
        raise ConfigurationError(f"{field} cannot contain traversal segments")
    return value.rstrip("/") or "/"


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
